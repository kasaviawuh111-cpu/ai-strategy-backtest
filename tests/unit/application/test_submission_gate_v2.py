from __future__ import annotations

import pytest

from ashare_lab.application.submission_gate_v2 import (
    V2SubmissionRejectedError,
    prepare_v2_backtest_submission,
)
from ashare_lab.domain.strategy.models_v2 import StrategySpecV2


def test_submission_gate_rejects_raw_strategy_model_or_payload() -> None:
    with pytest.raises(V2SubmissionRejectedError, match="ExecutableStrategyPlan"):
        prepare_v2_backtest_submission({"schema_version": "strategy.v2"})

    # Type-level guarantee: a parsed candidate DSL is not itself an executable plan.
    with pytest.raises(V2SubmissionRejectedError, match="ExecutableStrategyPlan"):
        prepare_v2_backtest_submission(StrategySpecV2.model_construct())
