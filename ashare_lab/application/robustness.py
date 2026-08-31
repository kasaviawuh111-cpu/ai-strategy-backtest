"""Predeclared execution-assumption stress tests with no performance selection."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from math import isfinite

from ashare_lab.application.daily_backtest import (
    DailyBacktestConfig,
    DailyBacktestInput,
    DailyBacktestResult,
    run_daily_backtest,
)
from ashare_lab.domain.strategy import canonical_hash


@dataclass(frozen=True, slots=True)
class ExecutionStressOutcome:
    scenario_id: str
    config_hash: str
    participation_rate: Decimal
    slippage_bps: Decimal
    total_return: float
    maximum_drawdown: float
    trade_count: int
    final_equity_cny: Decimal


@dataclass(frozen=True, slots=True)
class ExecutionRobustnessReport:
    profile: str
    scenarios: tuple[ExecutionStressOutcome, ...]

    def as_dict(self) -> dict[str, object]:
        returns = [item.total_return for item in self.scenarios]
        return {
            "profile": self.profile,
            "selectionPolicy": "predeclared_scenarios_no_optimization",
            "totalReturnRange": {"min": min(returns), "max": max(returns)},
            "scenarios": [
                {
                    "id": item.scenario_id,
                    "configHash": item.config_hash,
                    "participationRate": float(item.participation_rate),
                    "slippageBps": float(item.slippage_bps),
                    "totalReturn": _finite(item.total_return),
                    "maxDrawdown": _finite(item.maximum_drawdown),
                    "tradeCount": item.trade_count,
                    "finalEquityCny": float(item.final_equity_cny),
                }
                for item in self.scenarios
            ],
        }


def run_execution_robustness(
    request: DailyBacktestInput,
    *,
    base_result: DailyBacktestResult | None = None,
) -> ExecutionRobustnessReport:
    """Run the fixed v1 slippage/liquidity neighborhood in stable order."""

    base = request.config
    candidates = (
        ("base", base),
        ("slippage_1_5x", replace(base, slippage_bps=base.slippage_bps * Decimal("1.5"))),
        ("slippage_2x", replace(base, slippage_bps=base.slippage_bps * Decimal("2"))),
        ("participation_2_5pct", replace(base, participation_rate=Decimal("0.025"))),
        ("participation_5pct", replace(base, participation_rate=Decimal("0.05"))),
        ("participation_10pct", replace(base, participation_rate=Decimal("0.10"))),
    )
    outcomes: list[ExecutionStressOutcome] = []
    seen_config_hashes: set[str] = set()
    for scenario_id, scenario_config in candidates:
        config_hash = _config_hash(scenario_config)
        if config_hash in seen_config_hashes:
            continue
        seen_config_hashes.add(config_hash)
        result = (
            base_result
            if scenario_id == "base" and base_result is not None
            else run_daily_backtest(
                replace(
                    request,
                    run_key=f"{request.run_key}-{scenario_id}",
                    config=scenario_config,
                )
            )
        )
        outcomes.append(
            ExecutionStressOutcome(
                scenario_id=scenario_id,
                config_hash=config_hash,
                participation_rate=scenario_config.participation_rate,
                slippage_bps=scenario_config.slippage_bps,
                total_return=result.metrics.total_return,
                maximum_drawdown=result.metrics.maximum_drawdown,
                trade_count=result.metrics.trade_count,
                final_equity_cny=result.equity_curve[-1].equity,
            )
        )
    return ExecutionRobustnessReport(
        profile="execution.v1",
        scenarios=tuple(outcomes),
    )


def _config_hash(config: DailyBacktestConfig) -> str:
    return canonical_hash(
        {
            "allocationRatio": _decimal_text(config.allocation_ratio),
            "capacityMode": config.capacity_mode.value,
            "edgeEntryValiditySessions": config.edge_entry_validity_sessions,
            "eventEntryValiditySessions": config.event_entry_validity_sessions,
            "limitHandling": config.limit_handling.value,
            "maxExitAttempts": config.max_exit_attempts,
            "participationRate": _decimal_text(config.participation_rate),
            "retryUnfilledExits": config.retry_unfilled_exits,
            "slippageBps": _decimal_text(config.slippage_bps),
            "stateEntryValiditySessions": config.state_entry_validity_sessions,
        }
    )


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _finite(value: float) -> float:
    if not isfinite(value):
        raise ValueError("robustness report cannot contain non-finite metrics")
    return value
