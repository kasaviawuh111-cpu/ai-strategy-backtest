"""Type boundary between a Strategy DSL v2 candidate and future execution."""

from __future__ import annotations

from dataclasses import dataclass

from ashare_lab.domain.strategy.validation_v2 import (
    ExecutableStrategyPlan,
    is_validator_issued_plan,
)


class V2SubmissionRejectedError(TypeError):
    """Raised when raw DSL or model output attempts to bypass validation."""


@dataclass(frozen=True, slots=True)
class V2BacktestSubmission:
    """Validated hand-off envelope; no v2 engine integration exists in P0-B."""

    plan: ExecutableStrategyPlan


def prepare_v2_backtest_submission(value: object) -> V2BacktestSubmission:
    """Accept only a plan issued by the ordered Strategy v2 validator."""

    if not is_validator_issued_plan(value):
        raise V2SubmissionRejectedError(
            "v2 backtest submission requires a validator-issued ExecutableStrategyPlan"
        )
    return V2BacktestSubmission(plan=value)


__all__ = [
    "V2BacktestSubmission",
    "V2SubmissionRejectedError",
    "prepare_v2_backtest_submission",
]
