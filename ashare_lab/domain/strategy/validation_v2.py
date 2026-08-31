"""Fail-closed semantic validation and plan issuance for Strategy DSL v2.

The language model boundary ends at :class:`StrategyCandidateV2`.  Only this
module may issue an :class:`ExecutableStrategyPlan`, after the candidate has
passed the ordered schema, identity, capability, point-in-time, coverage and
grounding gates.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Literal, TypeGuard

from pydantic import ConfigDict, Field, ValidationError, field_validator

from ashare_lab.domain.catalog.models import CatalogSnapshot
from ashare_lab.domain.instruments import (
    InstrumentResolutionError,
    InstrumentResolver,
    SecurityMasterSnapshot,
)
from ashare_lab.domain.provenance import SourceRef
from ashare_lab.domain.strategy.canonical import canonical_hash, canonical_json
from ashare_lab.domain.strategy.models import (
    AllCondition,
    AnyCondition,
    BacktestConfig,
    CatalogRef,
    FirstOfExit,
    IndicatorCondition,
    Instrument,
    NotCondition,
    StrategySpec,
)
from ashare_lab.domain.strategy.validation import (
    StrategyCatalogError,
    validate_strategy_against_catalog,
)

from .models_v2 import (
    AllConditionV2,
    AnyConditionV2,
    BacktestConfigV2,
    ConditionGrounding,
    EventConditionV2,
    FinancialCondition,
    FrozenV2Model,
    LeafConditionV2,
    NotConditionV2,
    StrategySpecV2,
    TechnicalConditionV2,
    canonical_hash_v2,
    iter_condition_leaf_paths,
)

_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_SYMBOL = re.compile(r"[0-9]{6}\.(SH|SZ|BJ)")
_PLAN_AUTHORITY = object()
_PLAN_SIGNING_KEY = secrets.token_bytes(32)


class ValidationStage(StrEnum):
    SCHEMA = "schema"
    INSTRUMENT_IDENTITY = "instrument_identity"
    CAPABILITY = "capability"
    POINT_IN_TIME = "point_in_time"
    DATA_COVERAGE = "data_coverage"
    CONDITION_COMPLETENESS = "condition_completeness"


@dataclass(frozen=True, slots=True)
class StrategyV2ValidationIssue:
    stage: ValidationStage
    code: str
    path: str
    message: str


class StrategyV2ValidationError(ValueError):
    def __init__(self, issue: StrategyV2ValidationIssue) -> None:
        self.issue = issue
        super().__init__(f"{issue.stage.value}:{issue.code} at {issue.path}: {issue.message}")


class StrategyCandidateV2(FrozenV2Model):
    """The only shape accepted from a local parser or language model."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    candidate_type: Literal["strategy_candidate.v2"] = "strategy_candidate.v2"
    original_input: str = Field(min_length=1, max_length=20_000)
    draft_id: str = Field(pattern=r"^draft:[A-Za-z0-9_.-]{1,128}$")
    revision: int = Field(ge=1, le=1_000_000)
    provider: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    strategy: StrategySpecV2

    @field_validator("original_input")
    @classmethod
    def original_input_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("original_input cannot be blank")
        return value


@dataclass(frozen=True, slots=True)
class ConditionGroundingExpectation:
    """Trusted interpretation evidence, independent of the model candidate.

    It is produced by a deterministic parser or an explicit user confirmation,
    never copied from the candidate itself.  The condition hash detects a model
    that keeps the same source span while substituting different semantics.
    """

    dsl_path: str
    source_start: int
    source_end: int
    source_text: str
    condition_hash: str

    def __post_init__(self) -> None:
        if not self.dsl_path.startswith("$."):
            raise ValueError("grounding expectation path must be a DSL path")
        if self.source_start < 0 or self.source_end <= self.source_start:
            raise ValueError("grounding expectation source span is invalid")
        if not self.source_text:
            raise ValueError("grounding expectation source_text is required")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.condition_hash):
            raise ValueError("grounding expectation condition_hash is invalid")


@dataclass(frozen=True, slots=True)
class DatasetCoverageV2:
    """Pinned dataset evidence consumed by the PIT and coverage gates."""

    dataset_id: str
    instrument_symbol: str
    start: date
    end: date
    timezone: str
    availability_field: str
    retrieved_at_role: str
    missing_value_policy: str
    source_refs: tuple[SourceRef, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "dataset_id",
            "instrument_symbol",
            "timezone",
            "availability_field",
            "retrieved_at_role",
            "missing_value_policy",
        ):
            value = getattr(self, field_name)
            if type(value) is not str or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if _SYMBOL.fullmatch(self.instrument_symbol) is None:
            raise ValueError("instrument_symbol must be a canonical A-share symbol")
        if type(self.start) is not date or type(self.end) is not date or self.start > self.end:
            raise ValueError("dataset coverage period is invalid")
        if type(self.source_refs) is not tuple or not self.source_refs:
            raise ValueError("dataset coverage source_refs must be non-empty")
        if any(type(item) is not SourceRef for item in self.source_refs):
            raise ValueError("dataset coverage source_refs must contain SourceRef values")
        canonical = tuple(sorted(set(self.source_refs), key=lambda item: item.sort_key))
        object.__setattr__(self, "source_refs", canonical)

    @property
    def snapshot_ids(self) -> tuple[str, ...]:
        return tuple(sorted({item.snapshot_id for item in self.source_refs}))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "availability_field": self.availability_field,
            "dataset_id": self.dataset_id,
            "end": self.end.isoformat(),
            "instrument_symbol": self.instrument_symbol,
            "missing_value_policy": self.missing_value_policy,
            "retrieved_at_role": self.retrieved_at_role,
            "source_refs": [item.to_dict() for item in self.source_refs],
            "start": self.start.isoformat(),
            "timezone": self.timezone,
        }


@dataclass(frozen=True, slots=True)
class StrategyV2ValidationContext:
    security_master: SecurityMasterSnapshot
    original_input: str
    draft_id: str
    revision: int
    provider: str
    requested_instrument: str
    expected_backtest: BacktestConfigV2
    catalog: CatalogSnapshot
    dataset_coverage: tuple[DatasetCoverageV2, ...]
    grounding_expectations: tuple[ConditionGroundingExpectation, ...]
    code_revision: str

    def __post_init__(self) -> None:
        if type(self.original_input) is not str or not self.original_input.strip():
            raise ValueError("original_input must come from the immutable request envelope")
        if (
            type(self.draft_id) is not str
            or re.fullmatch(r"draft:[A-Za-z0-9_.-]{1,128}", self.draft_id) is None
        ):
            raise ValueError("draft_id must come from the trusted request envelope")
        if type(self.revision) is not int or not 1 <= self.revision <= 1_000_000:
            raise ValueError("revision must come from the trusted request envelope")
        if (
            type(self.provider) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.provider) is None
        ):
            raise ValueError("provider must come from the trusted request envelope")
        if type(self.requested_instrument) is not str or not self.requested_instrument.strip():
            raise ValueError("requested_instrument must be supplied by the trusted host context")
        if type(self.expected_backtest) is not BacktestConfigV2:
            raise ValueError("expected_backtest must come from the trusted request envelope")
        if type(self.code_revision) is not str or _GIT_SHA.fullmatch(self.code_revision) is None:
            raise ValueError("code_revision must be a 40-character Git SHA")
        if type(self.dataset_coverage) is not tuple:
            raise ValueError("dataset_coverage must be a tuple")
        if any(type(item) is not DatasetCoverageV2 for item in self.dataset_coverage):
            raise ValueError("dataset_coverage must contain DatasetCoverageV2 values")
        coverage_by_payload = {
            canonical_json(item.canonical_payload()): item for item in self.dataset_coverage
        }
        if len(coverage_by_payload) != len(self.dataset_coverage):
            raise ValueError("dataset_coverage must not contain duplicates")
        object.__setattr__(
            self,
            "dataset_coverage",
            tuple(coverage_by_payload[key] for key in sorted(coverage_by_payload)),
        )
        if type(self.grounding_expectations) is not tuple:
            raise ValueError("grounding_expectations must be a tuple")


@dataclass(frozen=True, slots=True)
class ExecutableStrategyPlan:
    """Validator-issued plan; it is not accepted from JSON or model output."""

    plan_id: str
    strategy: StrategySpecV2
    strategy_hash: str
    original_input: str
    draft_id: str
    revision: int
    provider: str
    catalog_hash: str
    security_master_snapshot_id: str
    dataset_coverage: tuple[DatasetCoverageV2, ...]
    data_snapshot_ids: tuple[str, ...]
    code_revision: str
    validation_stages: tuple[ValidationStage, ...]
    _authority: object = field(repr=False, compare=False)
    _receipt: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._authority is not _PLAN_AUTHORITY:
            raise ValueError("ExecutableStrategyPlan can only be issued by the v2 validator")
        if re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", self._receipt) is None:
            raise ValueError("ExecutableStrategyPlan validation receipt is invalid")

    def is_validator_issued(self) -> bool:
        """Prove this in-memory plan carries the module-private validation receipt."""

        expected = _sign_plan_payload(_plan_receipt_payload(self))
        return self._authority is _PLAN_AUTHORITY and hmac.compare_digest(
            self._receipt,
            expected,
        )


def condition_semantics_hash(condition: LeafConditionV2) -> str:
    """Hash one leaf without accepting natural-language claims as evidence."""

    return canonical_hash(condition.model_dump(mode="json"))


def validate_strategy_candidate_v2(
    candidate: object,
    context: StrategyV2ValidationContext,
) -> ExecutableStrategyPlan:
    """Validate in the required fail-fast order and issue an internal plan."""

    parsed = _parse_schema(candidate)
    _validate_instrument_identity(parsed, context)
    _validate_capability(parsed.strategy, context.catalog)
    _validate_pit_contract(context.dataset_coverage)
    _validate_data_coverage(parsed.strategy, context.dataset_coverage)
    _validate_condition_completeness(parsed, context)
    return _issue_plan(parsed, context)


def is_validator_issued_plan(value: object) -> TypeGuard[ExecutableStrategyPlan]:
    """Internal submission boundary used without exposing an executable flag."""

    return isinstance(value, ExecutableStrategyPlan) and value.is_validator_issued()


def _parse_schema(candidate: object) -> StrategyCandidateV2:
    try:
        payload = (
            candidate.model_dump(mode="json")
            if isinstance(candidate, StrategyCandidateV2)
            else candidate
        )
        return StrategyCandidateV2.model_validate(payload)
    except ValidationError as error:
        raise _failure(
            ValidationStage.SCHEMA,
            "schema_invalid",
            "/",
            str(error),
        ) from error


def _validate_instrument_identity(
    candidate: StrategyCandidateV2,
    context: StrategyV2ValidationContext,
) -> None:
    resolver = InstrumentResolver(context.security_master)
    try:
        resolved = resolver.resolve(
            context.requested_instrument,
            as_of=candidate.strategy.backtest.start,
        )
        resolved.require_tradable_on(candidate.strategy.backtest.end)
    except InstrumentResolutionError as error:
        raise _failure(
            ValidationStage.INSTRUMENT_IDENTITY,
            error.code,
            "/strategy/instrument",
            str(error),
        ) from error
    if resolved != candidate.strategy.instrument:
        raise _failure(
            ValidationStage.INSTRUMENT_IDENTITY,
            "instrument_identity_mismatch",
            "/strategy/instrument",
            "candidate instrument does not exactly match the authoritative security master",
        )


def _validate_capability(strategy: StrategySpecV2, catalog: CatalogSnapshot) -> None:
    leaves = tuple(iter_condition_leaf_paths(strategy))
    unsupported = next(
        (
            (path, condition)
            for path, condition in leaves
            if isinstance(condition, (FinancialCondition, EventConditionV2))
        ),
        None,
    )
    if unsupported is not None:
        path, condition = unsupported
        capability = "financial" if isinstance(condition, FinancialCondition) else "event"
        raise _failure(
            ValidationStage.CAPABILITY,
            "capability_unavailable",
            path,
            f"{capability} conditions are expressible in strategy.v2 but not executable yet",
        )

    try:
        validate_strategy_against_catalog(adapt_technical_strategy_v2_to_v1(strategy), catalog)
    except StrategyCatalogError as error:
        issue = error.issues[0]
        raise _failure(
            ValidationStage.CAPABILITY,
            issue.code,
            issue.path,
            issue.message,
        ) from error


def _validate_pit_contract(coverage: tuple[DatasetCoverageV2, ...]) -> None:
    if not coverage:
        raise _failure(
            ValidationStage.POINT_IN_TIME,
            "pit_evidence_missing",
            "/dataset_coverage",
            "at least one pinned dataset contract is required",
        )
    for index, item in enumerate(coverage):
        if (
            item.timezone != "Asia/Shanghai"
            or item.availability_field != "first_available_at"
            or item.retrieved_at_role != "audit_only"
            or item.missing_value_policy != "null_or_no_signal"
        ):
            raise _failure(
                ValidationStage.POINT_IN_TIME,
                "invalid_pit_policy",
                f"/dataset_coverage/{index}",
                "PIT data must use first_available_at, keep retrieved_at audit-only, "
                "and preserve missing values as null/no_signal",
            )


def _validate_data_coverage(
    strategy: StrategySpecV2,
    coverage: tuple[DatasetCoverageV2, ...],
) -> None:
    matches = tuple(
        item
        for item in coverage
        if item.dataset_id == "daily_ohlcv"
        and item.instrument_symbol == strategy.instrument.symbol
        and item.start <= strategy.backtest.start
        and item.end >= strategy.backtest.end
    )
    if not matches:
        raise _failure(
            ValidationStage.DATA_COVERAGE,
            "data_coverage_incomplete",
            "/dataset_coverage",
            "pinned daily_ohlcv does not cover the exact instrument and backtest period",
        )


def _validate_condition_completeness(
    candidate: StrategyCandidateV2,
    context: StrategyV2ValidationContext,
) -> None:
    strategy = candidate.strategy
    if (
        candidate.original_input != context.original_input
        or candidate.draft_id != context.draft_id
        or candidate.revision != context.revision
        or candidate.provider != context.provider
    ):
        raise _failure(
            ValidationStage.CONDITION_COMPLETENESS,
            "candidate_context_mismatch",
            "/candidate",
            "candidate audit metadata differs from the immutable request envelope",
        )
    if strategy.backtest != context.expected_backtest:
        raise _failure(
            ValidationStage.CONDITION_COMPLETENESS,
            "strategy_field_substituted",
            "/strategy/backtest",
            "candidate changed a trusted backtest period or capital setting",
        )
    interpretation = strategy.interpretation_coverage
    if interpretation.unmapped_material_spans:
        raise _failure(
            ValidationStage.CONDITION_COMPLETENESS,
            "unmapped_material_condition",
            "/strategy/interpretation_coverage/unmapped_material_spans",
            "one or more material user conditions were not mapped into the DSL",
        )

    leaf_by_path = dict(iter_condition_leaf_paths(strategy))
    grounding_by_path: dict[str, ConditionGrounding] = {}
    for grounding in interpretation.groundings:
        if grounding.dsl_path in grounding_by_path:
            raise _failure(
                ValidationStage.CONDITION_COMPLETENESS,
                "condition_grounding_duplicate",
                grounding.dsl_path,
                "a condition path must have exactly one source grounding",
            )
        grounding_by_path[grounding.dsl_path] = grounding

    if set(grounding_by_path) != set(leaf_by_path):
        raise _failure(
            ValidationStage.CONDITION_COMPLETENESS,
            "condition_grounding_incomplete",
            "/strategy/interpretation_coverage/groundings",
            "every and only executable condition leaves must be grounded",
        )

    expectations = context.grounding_expectations
    expectation_by_path = {item.dsl_path: item for item in expectations}
    if len(expectation_by_path) != len(expectations) or set(expectation_by_path) != set(
        leaf_by_path
    ):
        raise _failure(
            ValidationStage.CONDITION_COMPLETENESS,
            "trusted_grounding_incomplete",
            "/grounding_expectations",
            "trusted interpretation must cover every condition exactly once",
        )

    for path, condition in leaf_by_path.items():
        grounding = grounding_by_path[path]
        expected = expectation_by_path[path]
        actual_text = candidate.original_input[grounding.source_start : grounding.source_end]
        if actual_text != grounding.source_text:
            raise _failure(
                ValidationStage.CONDITION_COMPLETENESS,
                "source_span_mismatch",
                path,
                "candidate grounding is not an exact slice of original_input",
            )
        if (
            grounding.source_start != expected.source_start
            or grounding.source_end != expected.source_end
            or grounding.source_text != expected.source_text
        ):
            raise _failure(
                ValidationStage.CONDITION_COMPLETENESS,
                "condition_grounding_substituted",
                path,
                "candidate grounding differs from trusted interpretation evidence",
            )
        if condition_semantics_hash(condition) != expected.condition_hash:
            raise _failure(
                ValidationStage.CONDITION_COMPLETENESS,
                "condition_semantics_substituted",
                path,
                "candidate changed the semantics associated with the trusted source span",
            )


def _issue_plan(
    candidate: StrategyCandidateV2,
    context: StrategyV2ValidationContext,
) -> ExecutableStrategyPlan:
    snapshot_ids = tuple(
        sorted(
            {
                snapshot_id
                for coverage in context.dataset_coverage
                for snapshot_id in coverage.snapshot_ids
            }
        )
    )
    strategy_hash = canonical_hash_v2(candidate.strategy)
    plan_payload = {
        "catalog_hash": context.catalog.content_hash,
        "code_revision": context.code_revision,
        "data_coverage": [item.canonical_payload() for item in context.dataset_coverage],
        "draft_id": context.draft_id,
        "original_input": context.original_input,
        "provider": context.provider,
        "revision": context.revision,
        "security_master_snapshot_id": context.security_master.snapshot_id,
        "strategy_hash": strategy_hash,
        "validator_version": "strategy-v2-gate.1",
    }
    validation_stages = tuple(ValidationStage)
    receipt_payload = {
        "catalog_hash": context.catalog.content_hash,
        "code_revision": context.code_revision,
        "data_coverage": [item.canonical_payload() for item in context.dataset_coverage],
        "data_snapshot_ids": list(snapshot_ids),
        "draft_id": context.draft_id,
        "original_input": context.original_input,
        "plan_id": canonical_hash(plan_payload),
        "provider": context.provider,
        "revision": context.revision,
        "security_master_snapshot_id": context.security_master.snapshot_id,
        "strategy": candidate.strategy.model_dump(mode="json"),
        "strategy_hash": strategy_hash,
        "validation_stages": [stage.value for stage in validation_stages],
    }
    return ExecutableStrategyPlan(
        plan_id=canonical_hash(plan_payload),
        strategy=candidate.strategy,
        strategy_hash=strategy_hash,
        original_input=context.original_input,
        draft_id=context.draft_id,
        revision=context.revision,
        provider=context.provider,
        catalog_hash=context.catalog.content_hash,
        security_master_snapshot_id=context.security_master.snapshot_id,
        dataset_coverage=context.dataset_coverage,
        data_snapshot_ids=snapshot_ids,
        code_revision=context.code_revision,
        validation_stages=validation_stages,
        _authority=_PLAN_AUTHORITY,
        _receipt=_sign_plan_payload(receipt_payload),
    )


def _plan_receipt_payload(plan: ExecutableStrategyPlan) -> dict[str, object]:
    return {
        "catalog_hash": plan.catalog_hash,
        "code_revision": plan.code_revision,
        "data_coverage": [item.canonical_payload() for item in plan.dataset_coverage],
        "data_snapshot_ids": list(plan.data_snapshot_ids),
        "draft_id": plan.draft_id,
        "original_input": plan.original_input,
        "plan_id": plan.plan_id,
        "provider": plan.provider,
        "revision": plan.revision,
        "security_master_snapshot_id": plan.security_master_snapshot_id,
        "strategy": plan.strategy.model_dump(mode="json"),
        "strategy_hash": plan.strategy_hash,
        "validation_stages": [stage.value for stage in plan.validation_stages],
    }


def _sign_plan_payload(payload: Mapping[str, object]) -> str:
    digest = hmac.new(
        _PLAN_SIGNING_KEY,
        canonical_json(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"hmac-sha256:{digest}"


def adapt_technical_strategy_v2_to_v1(strategy: StrategySpecV2) -> StrategySpec:
    """Project a technical-only v2 strategy onto the unchanged v1 runtime DSL.

    This pure conversion is shared by validation and the P0-C execution bridge
    so capability checks and runtime execution cannot drift into two mappings.
    Non-technical conditions fail closed with ``TypeError``.
    """

    return StrategySpec(
        catalog=CatalogRef(
            catalog_id=strategy.catalog.catalog_id,
            release_version=strategy.catalog.release_version,
        ),
        instrument=Instrument(symbol=strategy.instrument.symbol),
        entry=_to_v1_condition(strategy.entry),
        exit=FirstOfExit(children=(_to_v1_condition(strategy.exit),)),
        backtest=BacktestConfig(
            start=strategy.backtest.start,
            end=strategy.backtest.end,
            initial_cash_cny=strategy.backtest.initial_cash_cny,
        ),
    )


def _to_v1_condition(
    condition: object,
) -> IndicatorCondition | AllCondition | AnyCondition | NotCondition:
    if isinstance(condition, TechnicalConditionV2):
        return IndicatorCondition(
            indicator_id=condition.indicator_id,
            definition_version=condition.definition_version,
            params=dict(condition.params),
            timeframe=condition.timeframe,
            evaluation_mode=condition.evaluation_mode,
            trigger=condition.trigger,
            value=condition.value,
        )
    if isinstance(condition, AllConditionV2):
        return AllCondition(children=tuple(_to_v1_condition(child) for child in condition.children))
    if isinstance(condition, AnyConditionV2):
        return AnyCondition(children=tuple(_to_v1_condition(child) for child in condition.children))
    if isinstance(condition, NotConditionV2):
        return NotCondition(child=_to_v1_condition(condition.child))
    raise TypeError("unsupported v2 condition reached the technical adapter")


def _failure(
    stage: ValidationStage,
    code: str,
    path: str,
    message: str,
) -> StrategyV2ValidationError:
    return StrategyV2ValidationError(
        StrategyV2ValidationIssue(stage=stage, code=code, path=path, message=message)
    )


__all__ = [
    "ConditionGroundingExpectation",
    "DatasetCoverageV2",
    "ExecutableStrategyPlan",
    "StrategyCandidateV2",
    "StrategyV2ValidationContext",
    "StrategyV2ValidationError",
    "StrategyV2ValidationIssue",
    "ValidationStage",
    "adapt_technical_strategy_v2_to_v1",
    "condition_semantics_hash",
    "is_validator_issued_plan",
    "validate_strategy_candidate_v2",
]
