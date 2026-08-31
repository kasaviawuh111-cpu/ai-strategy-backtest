"""Immutable Catalog release models for the Strategy DSL v1 subset."""

from __future__ import annotations

from datetime import date
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ashare_lab.domain.strategy.canonical import canonical_hash

type JsonScalar = str | int | float | bool


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ParameterDefinition(FrozenModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    value_type: Literal["integer", "number", "string", "boolean"]
    required: bool = True
    default: JsonScalar | None = None
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[JsonScalar, ...] = ()

    @model_validator(mode="after")
    def bounds_are_valid(self) -> ParameterDefinition:
        if self.minimum is not None and not isfinite(self.minimum):
            raise ValueError("parameter minimum must be finite")
        if self.maximum is not None and not isfinite(self.maximum):
            raise ValueError("parameter maximum must be finite")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("parameter minimum cannot exceed maximum")
        if self.default is not None and not _matches_type(self.default, self.value_type):
            raise ValueError(f"default for {self.name!r} does not match {self.value_type}")
        if self.choices and self.default is not None and self.default not in self.choices:
            raise ValueError(f"default for {self.name!r} is not in choices")
        return self


class ParameterRelation(FrozenModel):
    left: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    op: Literal["lt", "lte", "gt", "gte"]
    right: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


class TriggerDefinition(FrozenModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    value_requirement: Literal["required", "forbidden"]
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: bool = False
    exclusive_maximum: bool = False

    @model_validator(mode="after")
    def bounds_are_valid(self) -> TriggerDefinition:
        if self.minimum is not None and not isfinite(self.minimum):
            raise ValueError("trigger minimum must be finite")
        if self.maximum is not None and not isfinite(self.maximum):
            raise ValueError("trigger maximum must be finite")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("trigger minimum cannot exceed maximum")
        return self


class IndicatorDefinition(FrozenModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    status: Literal["stable", "experimental", "unavailable"]
    warmup_bars: int = Field(ge=0, le=100_000)
    timeframes: tuple[Literal["1d"], ...] = Field(min_length=1)
    evaluation_modes: tuple[Literal["bar_close_confirmed"], ...] = Field(min_length=1)
    parameters: tuple[ParameterDefinition, ...] = ()
    parameter_relations: tuple[ParameterRelation, ...] = ()
    triggers: tuple[TriggerDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def members_are_unique_and_resolved(self) -> IndicatorDefinition:
        parameter_names = [parameter.name for parameter in self.parameters]
        trigger_ids = [trigger.id for trigger in self.triggers]
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError(f"indicator {self.id!r} contains duplicate parameter names")
        if len(trigger_ids) != len(set(trigger_ids)):
            raise ValueError(f"indicator {self.id!r} contains duplicate trigger ids")
        known = set(parameter_names)
        for relation in self.parameter_relations:
            if relation.left not in known or relation.right not in known:
                raise ValueError(f"indicator {self.id!r} relation references unknown parameter")
        return self


class CatalogManifest(FrozenModel):
    schema_version: Literal["indicator-catalog.v1"] = "indicator-catalog.v1"
    catalog_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,63}$")
    release_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    state: Literal["active", "retired"]
    published_on: date
    indicators: tuple[IndicatorDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def indicator_ids_are_unique(self) -> CatalogManifest:
        ids = [indicator.id for indicator in self.indicators]
        if len(ids) != len(set(ids)):
            raise ValueError("catalog manifest contains duplicate indicator ids")
        return self

    @property
    def content_hash(self) -> str:
        return canonical_hash(self)


class CatalogSnapshot(FrozenModel):
    manifests: tuple[CatalogManifest, ...] = Field(min_length=1)
    indicators: tuple[IndicatorDefinition, ...] = Field(min_length=1)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    def resolve_indicator(self, indicator_id: str) -> IndicatorDefinition | None:
        return next((item for item in self.indicators if item.id == indicator_id), None)

    def contains_release(self, catalog_id: str, release_version: str) -> bool:
        return any(
            manifest.catalog_id == catalog_id and manifest.release_version == release_version
            for manifest in self.manifests
        )


def _matches_type(value: JsonScalar, expected: str) -> bool:
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    return False
