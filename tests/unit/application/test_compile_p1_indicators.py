from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.application.compile_strategy import CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.strategy import (
    IndicatorCondition,
    PositionReturnExit,
    TrailingDrawdownExit,
)
from ashare_lab.ports.candidate_generation import CompileInput

ROOT = Path(__file__).parents[3]


@pytest.fixture
def compiler() -> StrategyCompiler:
    return StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
        trusted_date_provider=lambda: date(2026, 8, 30),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "entry_id", "entry_trigger", "exit_trigger"),
    [
        ("真实波幅高于3买入，低于1卖出", "price.true_range", "above", "below"),
        ("ATR(14)高于3买入，ATR低于2卖出", "technical.atr", "above", "below"),
        ("NATR14高于5买入，NATR低于3卖出", "technical.natr", "above", "below"),
        ("ADX14高于25买入，ADX低于20卖出", "technical.adx", "above", "below"),
        (
            "+DI上穿-DI买入，+DI下穿-DI卖出",
            "technical.dmi",
            "plus_crosses_above_minus",
            "plus_crosses_below_minus",
        ),
        ("20日BIAS低于-5买入，BIAS高于5卖出", "technical.bias", "below", "above"),
        ("12日ROC上穿0买入，ROC下穿0卖出", "technical.roc", "crosses_above", "crosses_below"),
        ("10日MOM高于0买入，MOM低于0卖出", "technical.momentum", "above", "below"),
        (
            "StochasticK上穿D买入，StochasticK下穿D卖出",
            "technical.stochastic",
            "k_crosses_above_d",
            "k_crosses_below_d",
        ),
        (
            "Williams%R低于-80买入，Williams%R高于-20卖出",
            "technical.williams_r",
            "below",
            "above",
        ),
        (
            "股价突破20日唐奇安上轨买入，跌破20日唐奇安下轨卖出",
            "technical.donchian",
            "price_crosses_above_upper",
            "price_crosses_below_lower",
        ),
        (
            "20日收益率标准差高于3买入，低于2卖出",
            "technical.return_stddev",
            "above",
            "below",
        ),
        (
            "20日历史波动率高于30买入，低于20卖出",
            "technical.historical_volatility",
            "above",
            "below",
        ),
    ],
)
async def test_p1_chinese_rules_compile_to_catalog_conditions(
    compiler: StrategyCompiler,
    utterance: str,
    entry_id: str,
    entry_trigger: str,
    exit_trigger: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY, outcome.diagnostic_code
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == entry_id
    assert outcome.strategy.entry.trigger == entry_trigger
    exit_condition = outcome.strategy.exit.children[0]
    assert isinstance(exit_condition, IndicatorCondition)
    assert exit_condition.indicator_id == entry_id
    assert exit_condition.trigger == exit_trigger


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "indicator_id"),
    [
        ("20日+DI上穿-DI买入，20日+DI下穿-DI卖出", "technical.dmi"),
        ("20日WR低于-80买入，20日WR高于-20卖出", "technical.williams_r"),
    ],
)
async def test_p1_di_and_wr_prefix_periods_are_preserved(
    compiler: StrategyCompiler,
    utterance: str,
    indicator_id: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY, outcome.diagnostic_code
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == indicator_id
    assert outcome.strategy.entry.params["period"] == 20
    exit_condition = outcome.strategy.exit.children[0]
    assert isinstance(exit_condition, IndicatorCondition)
    assert exit_condition.indicator_id == indicator_id
    assert exit_condition.params["period"] == 20


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    [
        "ATR买入，ATR卖出",
        "DMI买入，DMI卖出",
        "Stochastic买入，Stochastic卖出",
        "唐奇安通道买入，唐奇安通道卖出",
    ],
)
async def test_p1_ambiguous_direction_or_threshold_fails_closed(
    compiler: StrategyCompiler,
    utterance: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.strategy is None
    assert outcome.diagnostic_code == "indicator_trigger_requires_clarification"
    assert outcome.clarification is not None
    assert "什么情况下触发交易" in outcome.clarification


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    [
        "matrix高于3买入，matrix低于2卖出",
        "process高于3买入，process低于2卖出",
        "unbiased高于3买入，unbiased低于2卖出",
        "admin高于3买入，admin低于2卖出",
        "stochasticity高于3买入，stochasticity低于2卖出",
    ],
)
async def test_p1_english_aliases_do_not_match_inside_unrelated_words(
    compiler: StrategyCompiler,
    utterance: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is not CompileStatus.READY
    assert outcome.strategy is None


@pytest.mark.asyncio
async def test_host_instrument_context_cannot_be_overridden_by_utterance(
    compiler: StrategyCompiler,
) -> None:
    mismatch = await compiler.compile(
        CompileInput(
            utterance="600519.SH的ATR高于3买入，ATR低于2卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )
    same = await compiler.compile(
        CompileInput(
            utterance="300059.SZ的ATR高于3买入，ATR低于2卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert mismatch.status is CompileStatus.UNSUPPORTED
    assert mismatch.diagnostic_code == "instrument_context_mismatch"
    assert same.status is CompileStatus.READY
    assert same.strategy is not None and same.strategy.instrument.symbol == "300059.SZ"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("risk_text", "expected_type", "expected_trigger", "expected_threshold"),
    [
        ("止盈20%", PositionReturnExit, "take_profit", 20),
        ("止损8%", PositionReturnExit, "stop_loss", 8),
        ("从高点回撤10%", TrailingDrawdownExit, None, 10),
    ],
)
async def test_explicit_risk_exit_is_first_of_alongside_the_original_market_exit(
    compiler: StrategyCompiler,
    risk_text: str,
    expected_type: type[PositionReturnExit] | type[TrailingDrawdownExit],
    expected_trigger: str | None,
    expected_threshold: int,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=f"MACD金叉买入，MACD死叉或{risk_text}卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY, outcome.diagnostic_code
    assert outcome.strategy is not None
    exits = outcome.strategy.exit.children
    assert any(
        isinstance(item, IndicatorCondition)
        and item.indicator_id == "technical.macd"
        and item.trigger == "death_cross"
        for item in exits
    )
    risk = next(item for item in exits if isinstance(item, expected_type))
    assert risk.threshold_pct == expected_threshold
    if isinstance(risk, PositionReturnExit):
        assert risk.trigger == expected_trigger


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("risk_text", "diagnostic_code"),
    [
        ("止盈", "take_profit_threshold_required"),
        ("止损", "stop_loss_threshold_required"),
        ("从高点回撤", "trailing_drawdown_threshold_required"),
    ],
)
async def test_risk_exit_without_an_explicit_percent_fails_closed(
    compiler: StrategyCompiler,
    risk_text: str,
    diagnostic_code: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=f"MACD金叉买入，MACD死叉或{risk_text}卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.strategy is None
    assert outcome.diagnostic_code == diagnostic_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    [
        "MACD金叉买入，止盈20%且止损5%卖出",
        "MACD金叉买入，止损5%并且止盈20%卖出",
        "MACD金叉买入，止盈20%同时止损5%卖出",
    ],
)
async def test_contradictory_position_returns_cannot_become_first_of(
    compiler: StrategyCompiler,
    utterance: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.INVALID
    assert outcome.strategy is None
    assert outcome.diagnostic_code == "strategy_validation_failed:ValidationError"
    assert {item.diagnostic_code for item in outcome.candidate_rejections} == {
        "strategy_validation_failed:ValidationError"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "child_types"),
    [
        (
            "MACD金叉买入，MACD死叉且止损5%卖出",
            {"indicator_condition", "position_return_exit"},
        ),
        (
            "MACD金叉买入，持有3个交易日且止盈20%卖出",
            {"holding_period_exit", "position_return_exit"},
        ),
    ],
)
async def test_supported_position_aware_and_preserves_all_conditions(
    compiler: StrategyCompiler,
    utterance: str,
    child_types: set[str],
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY, outcome.diagnostic_code
    assert outcome.strategy is not None
    assert outcome.strategy.exit.op == "all"
    children = outcome.strategy.exit.children
    assert len(children) == 2
    assert {child.type for child in children} == child_types
    for child in children:
        if child.type == "holding_period_exit":
            assert child.sessions == 3
        elif isinstance(child, PositionReturnExit):
            assert (child.trigger, child.threshold_pct) == (
                ("stop_loss", 5.0) if "止损" in utterance else ("take_profit", 20.0)
            )
        else:
            assert isinstance(child, IndicatorCondition)
            assert (child.indicator_id, child.trigger) == ("technical.macd", "death_cross")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    [
        "MACD金叉买入，止盈20%或止损5%卖出",
        "MACD金叉买入，止损5%或者止盈20%卖出",
    ],
)
async def test_position_aware_exit_or_remains_first_trigger_wins(
    compiler: StrategyCompiler,
    utterance: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY, outcome.diagnostic_code
    assert outcome.strategy is not None
    exits = outcome.strategy.exit.children
    assert len(exits) == 2
    assert {item.trigger for item in exits if isinstance(item, PositionReturnExit)} == {
        "take_profit",
        "stop_loss",
    }
