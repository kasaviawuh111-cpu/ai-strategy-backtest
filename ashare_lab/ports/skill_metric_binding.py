"""Meaning-level identity check between requested metrics and returned metadata.

This boundary has no model transport or market-data implementation dependency.
A matched verdict does not replace the caller's unit, security or date checks.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

type MetricBindingInput = tuple[str, Mapping[str, object]]
type BindingVerdict = Literal["matched", "mismatch", "uncertain"]
type ReasonCode = Literal[
    "same_metric", "different_metric", "different_parameters", "different_basis",
    "missing_identity", "missing_parameters", "ambiguous_metadata", "review_unavailable",
]


@dataclass(frozen=True, slots=True)
class SkillMetricBindingVerdict:
    verdict: BindingVerdict
    reason_code: ReasonCode
    reason: str
    binding_hash: str
    provider: str
    model: str
    prompt_version: str
    cached: bool = False

    @property
    def matched(self) -> bool:
        return self.verdict == "matched"


class MetricBindingReviewer(Protocol):
    """Return one verdict per binding in input order, without changing input data."""

    async def verify(
        self, bindings: tuple[MetricBindingInput, ...],
    ) -> tuple[SkillMetricBindingVerdict, ...]: ...
