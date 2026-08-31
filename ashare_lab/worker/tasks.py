"""RQ-callable task functions; payloads contain only durable run identities."""

from __future__ import annotations

from functools import lru_cache

from ashare_lab.application.execute_backtest import BacktestExecutionService
from ashare_lab.bootstrap import build_execution_runtime
from ashare_lab.domain.shared import RunId


@lru_cache(maxsize=1)
def _executor() -> BacktestExecutionService:
    return build_execution_runtime().executor


def execute_backtest_job(run_id: str) -> str:
    """Load immutable inputs from storage and execute one idempotent run."""

    completed = _executor().execute(RunId(run_id))
    return completed.run_id.value
