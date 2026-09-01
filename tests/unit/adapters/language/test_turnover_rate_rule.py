from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.application.compile_strategy import CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.strategy import IndicatorCondition
from ashare_lab.ports.candidate_generation import CompileInput

ROOT = Path(__file__).parents[4]


@pytest.mark.asyncio
async def test_turnover_rate_percent_thresholds_compile_without_volume_inference() -> None:
    """换手率必须是供应商原始百分数, 不得改写为成交量或小数比例。"""

    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
        trusted_date_provider=lambda: date(2026, 8, 30),
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="换手率高于3%买入，低于1%卖出，回测近1年",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY, outcome.diagnostic_code
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == "market.turnover_rate"
    assert outcome.strategy.entry.trigger == "above"
    assert outcome.strategy.entry.value == 3
    assert outcome.strategy.entry.params == {}
    exit_condition = outcome.strategy.exit.children[0]
    assert isinstance(exit_condition, IndicatorCondition)
    assert exit_condition.indicator_id == "market.turnover_rate"
    assert exit_condition.trigger == "below"
    assert exit_condition.value == 1


@pytest.mark.asyncio
async def test_turnover_rate_rejects_a_threshold_without_a_percentage_unit() -> None:
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
        trusted_date_provider=lambda: date(2026, 8, 30),
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="换手率高于3买入，低于1卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "turnover_rate_percentage_required"
