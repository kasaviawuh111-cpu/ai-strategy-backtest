from datetime import date
from pathlib import Path

import pytest

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.application.compile_strategy import CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.ports.candidate_generation import CompileInput

ROOT = Path(__file__).parents[3]


@pytest.fixture
def compiler() -> StrategyCompiler:
    return StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.08.30",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    [
        "东方财富 MACD 金叉买入，死叉卖出，回测 2021-08-06 至 2026-08-06",
        "东方财富 MACD 金叉买入，死叉卖出，回测 2021年8月6日至2026年8月6日",
        "东方财富年度报告发布后买入，MACD死叉卖出，回测2021/08/06到2026/08/06",
    ],
)
async def test_explicit_backtest_date_range_is_preserved(
    compiler: StrategyCompiler,
    utterance: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 29),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.backtest.start == date(2021, 8, 6)
    assert outcome.strategy.backtest.end == date(2026, 8, 6)
    provenance = {item.path: item.source for item in outcome.provenance}
    assert provenance["/backtest/start"] == "utterance/explicit_date_range"
    assert provenance["/backtest/end"] == "utterance/explicit_date_range"


@pytest.mark.asyncio
async def test_relative_backtest_years_are_not_replaced_by_default(
    compiler: StrategyCompiler,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="MACD金叉买入，死叉卖出，回测近3年",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 29),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.backtest.start == date(2023, 8, 29)
    assert outcome.strategy.backtest.end == date(2026, 8, 29)
    provenance = {item.path: item.source for item in outcome.provenance}
    assert provenance["/backtest/start"] == "utterance/relative_lookback"
    assert provenance["/backtest/end"] == "request/as_of_date"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("period", "diagnostic_code"),
    [
        ("回测2021-02-30至2026-08-06", "backtest_date_invalid"),
        ("回测2021-08-06至", "backtest_date_range_incomplete"),
        ("回测2021-08-06", "backtest_date_range_incomplete"),
        ("回测2026-08-06至2021-08-06", "backtest_date_range_reversed"),
        (
            "回测2021-08-06到2022-08-06至2026-08-06",
            "backtest_date_range_ambiguous",
        ),
        (
            "回测近5年，回测2021-08-06至2026-08-06",
            "backtest_date_range_ambiguous",
        ),
        ("回测2021年至2026年", "backtest_date_range_unsupported"),
        ("回测2021-08至2026-08", "backtest_date_range_unsupported"),
        ("回测近三年", "backtest_date_range_unsupported"),
        ("回测近0年", "backtest_lookback_invalid"),
        ("回测近101年", "backtest_lookback_invalid"),
    ],
)
async def test_incomplete_invalid_or_ambiguous_period_fails_closed(
    compiler: StrategyCompiler,
    period: str,
    diagnostic_code: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=f"MACD金叉买入，死叉卖出，{period}",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 29),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.strategy is None
    assert outcome.diagnostic_code == diagnostic_code


@pytest.mark.asyncio
async def test_explicit_end_after_as_of_date_fails_closed(
    compiler: StrategyCompiler,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="MACD金叉买入，死叉卖出，回测2021-08-06至2026-08-30",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 29),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.strategy is None
    assert outcome.diagnostic_code == "backtest_end_after_as_of_date"
