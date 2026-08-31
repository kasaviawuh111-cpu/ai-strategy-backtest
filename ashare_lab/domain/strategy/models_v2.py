"""Strict, non-executable Strategy DSL v2 value models.

This module describes candidate strategy intent only.  It deliberately has no
``executable`` or order/signal fields: a separate semantic gate must validate a
candidate before it can become an executable plan.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from datetime import date
from math import isfinite
from typing import Annotated, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from ashare_lab.domain.instruments.models import InstrumentRef

type JsonScalarV2 = StrictBool | StrictInt | StrictFloat | StrictStr
type NumericScalar = StrictInt | StrictFloat


class FrozenV2Model(BaseModel):
    """Strict immutable base for all v2 contract values."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class CatalogRefV2(FrozenV2Model):
    catalog_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,63}$")
    release_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class TechnicalConditionV2(FrozenV2Model):
    """Daily, close-confirmed technical condition aligned with the v1 catalog."""

    type: Literal["technical"] = "technical"
    indicator_id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    definition_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    params: dict[str, JsonScalarV2] = Field(default_factory=dict, max_length=16)
    timeframe: Literal["1d"] = "1d"
    evaluation_mode: Literal["bar_close_confirmed"] = "bar_close_confirmed"
    trigger: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    value: NumericScalar | None = None

    @field_validator("params")
    @classmethod
    def params_are_bounded_and_finite(
        cls,
        params: dict[str, JsonScalarV2],
    ) -> dict[str, JsonScalarV2]:
        for name, value in params.items():
            if not name or not name.replace("_", "a").isalnum() or not name[0].isalpha():
                raise ValueError(f"invalid parameter name: {name!r}")
            if isinstance(value, float) and not isfinite(value):
                raise ValueError(f"parameter {name!r} must be finite")
        return params

    @field_validator("value")
    @classmethod
    def value_is_finite(cls, value: NumericScalar | None) -> NumericScalar | None:
        if isinstance(value, bool):
            raise ValueError("technical comparison value must be numeric")
        if isinstance(value, float) and not isfinite(value):
            raise ValueError("technical comparison value must be finite")
        return value


class FinancialCondition(FrozenV2Model):
    """Typed financial predicate; expressible here but not executable yet."""

    type: Literal["financial"] = "financial"
    metric_id: str = Field(pattern=r"^financial\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
    definition_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    report_type: Literal["q1", "semiannual", "q3", "annual"]
    period_basis: Literal[
        "instant",
        "single_quarter",
        "ytd_cumulative",
        "full_year",
        "ttm",
        "period_end_point",
    ]
    statement_scope: Literal["consolidated", "parent_company"]
    revision_policy: Literal["as_known_at_signal"] = "as_known_at_signal"
    comparator: Literal["gt", "gte", "lt", "lte", "eq", "ne"]
    value: NumericScalar
    unit: Literal["CNY", "CNY_PER_SHARE", "PERCENT", "RATIO", "TIMES"]

    @field_validator("value")
    @classmethod
    def value_is_finite(cls, value: NumericScalar) -> NumericScalar:
        if isinstance(value, bool):
            raise ValueError("financial comparison value must be numeric")
        if isinstance(value, float) and not isfinite(value):
            raise ValueError("financial comparison value must be finite")
        return value


class EventAttributePredicate(FrozenV2Model):
    """One typed, bounded event-attribute comparison."""

    attribute_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    comparator: Literal["gt", "gte", "lt", "lte", "eq", "ne"]
    value: JsonScalarV2
    value_type: Literal["text", "integer", "number", "boolean"]
    unit: Literal["NONE", "CNY", "PERCENT", "RATIO", "COUNT"] = "NONE"

    @model_validator(mode="after")
    def value_matches_declared_type(self) -> EventAttributePredicate:
        value = self.value
        valid = (
            (self.value_type == "text" and isinstance(value, str))
            or (
                self.value_type == "integer"
                and isinstance(value, int)
                and not isinstance(value, bool)
            )
            or (
                self.value_type == "number"
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            )
            or (self.value_type == "boolean" and isinstance(value, bool))
        )
        if not valid:
            raise ValueError("event attribute value does not match value_type")
        if isinstance(value, float) and not isfinite(value):
            raise ValueError("event attribute value must be finite")
        if self.value_type in {"text", "boolean"} and self.comparator not in {"eq", "ne"}:
            raise ValueError("text and boolean event attributes support only eq/ne")
        if self.value_type == "text" and self.unit != "NONE":
            raise ValueError("text event attributes cannot declare a numeric unit")
        return self


class EventConditionV2(FrozenV2Model):
    """Catalog-coded event predicate; expressible here but not executable yet."""

    type: Literal["event"] = "event"
    event_code: str = Field(pattern=r"^event\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    definition_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    trigger: Literal["became_available"] = "became_available"
    occurrence_scope: Literal[
        "initial_validated_publication",
        "revision",
        "withdrawal",
    ] = "initial_validated_publication"
    availability_mode: Literal["validated_first_available_at"] = "validated_first_available_at"
    attributes: tuple[EventAttributePredicate, ...] = Field(default_factory=tuple, max_length=8)


class AllConditionV2(FrozenV2Model):
    type: Literal["all"] = "all"
    children: tuple[ConditionV2, ...] = Field(min_length=2, max_length=16)


class AnyConditionV2(FrozenV2Model):
    type: Literal["any"] = "any"
    children: tuple[ConditionV2, ...] = Field(min_length=2, max_length=16)


class NotConditionV2(FrozenV2Model):
    type: Literal["not"] = "not"
    child: ConditionV2


type ConditionV2 = Annotated[
    TechnicalConditionV2
    | FinancialCondition
    | EventConditionV2
    | AllConditionV2
    | AnyConditionV2
    | NotConditionV2,
    Field(discriminator="type"),
]
type LeafConditionV2 = TechnicalConditionV2 | FinancialCondition | EventConditionV2


class SourceSpan(FrozenV2Model):
    """A literal span in the user's immutable original input."""

    # Source evidence must remain byte-for-byte faithful to the submitted text.
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    source_start: int = Field(ge=0, le=1_000_000)
    source_end: int = Field(ge=1, le=1_000_000)
    source_text: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def span_is_ordered(self) -> SourceSpan:
        if self.source_end <= self.source_start:
            raise ValueError("source_end must be greater than source_start")
        if not self.source_text.strip():
            raise ValueError("source_text cannot be blank")
        return self


class ConditionGrounding(SourceSpan):
    """Maps one DSL node to the exact words that caused it to exist."""

    dsl_path: str = Field(
        pattern=r"^\$\.(entry|exit)(?:\.children\[[0-9]+\]|\.child)*$",
        max_length=512,
    )


class InterpretationCoverage(FrozenV2Model):
    """Candidate-level evidence used by the later semantic completeness gate."""

    groundings: tuple[ConditionGrounding, ...] = Field(default_factory=tuple, max_length=128)
    unmapped_material_spans: tuple[SourceSpan, ...] = Field(default_factory=tuple, max_length=32)


class BacktestConfigV2(FrozenV2Model):
    start: date
    end: date
    initial_cash_cny: int = Field(gt=0, le=1_000_000_000)

    @model_validator(mode="after")
    def period_is_ordered(self) -> BacktestConfigV2:
        if self.start > self.end:
            raise ValueError("backtest start must be on or before end")
        return self


class StrategySpecV2(FrozenV2Model):
    """A bounded candidate DSL; validation does not imply executability."""

    schema_version: Literal["strategy.v2"] = "strategy.v2"
    catalog: CatalogRefV2
    instrument: InstrumentRef
    entry: ConditionV2
    exit: ConditionV2
    interpretation_coverage: InterpretationCoverage
    backtest: BacktestConfigV2

    @model_validator(mode="after")
    def condition_tree_is_bounded(self) -> StrategySpecV2:
        roots = (self.entry, self.exit)
        node_count = sum(_condition_size(root) for root in roots)
        max_depth = max(_condition_depth(root) for root in roots)
        if node_count > 64:
            raise ValueError("strategy condition tree exceeds 64 nodes")
        if max_depth > 8:
            raise ValueError("strategy condition tree exceeds depth 8")
        return self


def _children(condition: ConditionV2) -> tuple[ConditionV2, ...]:
    if isinstance(condition, (AllConditionV2, AnyConditionV2)):
        return condition.children
    if isinstance(condition, NotConditionV2):
        return (condition.child,)
    return ()


def _condition_size(condition: ConditionV2) -> int:
    return 1 + sum(_condition_size(child) for child in _children(condition))


def _condition_depth(condition: ConditionV2) -> int:
    children = _children(condition)
    return 1 if not children else 1 + max(_condition_depth(child) for child in children)


def _iter_leaf_paths(
    condition: ConditionV2,
    path: str,
) -> Iterator[tuple[str, LeafConditionV2]]:
    if isinstance(condition, (TechnicalConditionV2, FinancialCondition, EventConditionV2)):
        yield path, condition
        return
    if isinstance(condition, NotConditionV2):
        yield from _iter_leaf_paths(condition.child, f"{path}.child")
        return
    for index, child in enumerate(condition.children):
        yield from _iter_leaf_paths(child, f"{path}.children[{index}]")


def iter_condition_leaf_paths(
    spec: StrategySpecV2,
) -> Iterator[tuple[str, LeafConditionV2]]:
    """Yield every condition leaf in stable document order with its DSL path."""

    yield from _iter_leaf_paths(spec.entry, "$.entry")
    yield from _iter_leaf_paths(spec.exit, "$.exit")


def _canonical_value(value: object) -> object:
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("canonical Strategy DSL cannot contain non-finite numbers")
        return int(value) if value.is_integer() else value
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): _canonical_value(item) for key, item in mapping.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        return [_canonical_value(item) for item in sequence]
    return value


def canonical_json_v2(spec: StrategySpecV2) -> str:
    """Serialize a v2 strategy deterministically for hashing and audit."""

    payload = _canonical_value(spec.model_dump(mode="json"))
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_hash_v2(spec: StrategySpecV2) -> str:
    """Return a content hash over the deterministic v2 serialization."""

    digest = hashlib.sha256(canonical_json_v2(spec).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


__all__ = [
    "AllConditionV2",
    "AnyConditionV2",
    "BacktestConfigV2",
    "CatalogRefV2",
    "ConditionGrounding",
    "ConditionV2",
    "EventAttributePredicate",
    "EventConditionV2",
    "FinancialCondition",
    "InterpretationCoverage",
    "LeafConditionV2",
    "NotConditionV2",
    "SourceSpan",
    "StrategySpecV2",
    "TechnicalConditionV2",
    "canonical_hash_v2",
    "canonical_json_v2",
    "iter_condition_leaf_paths",
]
