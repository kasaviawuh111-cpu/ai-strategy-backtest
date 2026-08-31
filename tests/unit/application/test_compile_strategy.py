from datetime import date
from pathlib import Path

import pytest

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.adapters.language.rule_based import _ANNOUNCEMENT_EVENT_DEFINITIONS
from ashare_lab.application.compile_strategy import CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.events.catalog import EXECUTABLE_EVENT_DEFINITIONS
from ashare_lab.domain.strategy import (
    AllCondition,
    AnyCondition,
    EventCondition,
    HoldingPeriodExit,
    IndicatorCondition,
)
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
async def test_macd_sentence_compiles_without_unnecessary_question(
    compiler: StrategyCompiler,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="MACD 金叉买入，死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.clarification is None
    assert outcome.strategy is not None
    assert outcome.strategy.instrument.symbol == "300059.SZ"
    assert outcome.strategy.backtest.start == date(2021, 8, 27)
    assert outcome.strategy.backtest.initial_cash_cny == 1_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "indicator_id", "entry_trigger", "exit_trigger"),
    [
        ("MACD金叉买入，死叉卖出", "technical.macd", "golden_cross", "death_cross"),
        ("RSI低于30买入，高于70卖出", "technical.rsi", "below", "above"),
        (
            "股价突破20日均线买入，跌破20日均线卖出",
            "technical.ma",
            "price_crosses_above",
            "price_crosses_below",
        ),
        ("KDJ金叉买入，死叉卖出", "technical.kdj", "golden_cross", "death_cross"),
        (
            "股价突破布林线上轨买入，跌破布林线中轨卖出",
            "technical.bollinger",
            "price_crosses_above_upper",
            "price_crosses_below_middle",
        ),
        ("OBV上升买入，下降卖出", "technical.obv", "rising", "falling"),
        (
            "放量上涨5%买入，放量下跌5%卖出",
            "volume.price_confirmation",
            "surge_up",
            "surge_down",
        ),
    ],
)
async def test_common_daily_technical_utterances_preserve_direction(
    compiler: StrategyCompiler,
    utterance: str,
    indicator_id: str,
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

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == indicator_id
    assert outcome.strategy.entry.trigger == entry_trigger
    assert len(outcome.strategy.exit.children) == 1
    exit_condition = outcome.strategy.exit.children[0]
    assert isinstance(exit_condition, IndicatorCondition)
    assert exit_condition.indicator_id == indicator_id
    assert exit_condition.trigger == exit_trigger


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "term", "comparator", "value"),
    [
        ("同花顺发年报提到ai次数超过5次的话就买入，3天后卖出", "ai", "gt", 5),
        ("年报提到AI超过5次买入，持有3个交易日卖出", "AI", "gt", 5),
        ("年报正文AI出现至少5次买入，买入后3个交易日卖出", "AI", "gte", 5),
        (
            "完整年报里人工智能至少出现5次买入，成交后第3个交易日卖出",
            "人工智能",
            "gte",
            5,
        ),
    ],
)
async def test_periodic_report_term_count_and_fill_anchored_exit_compile(
    compiler: StrategyCompiler,
    utterance: str,
    term: str,
    comparator: str,
    value: int,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300033.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.instrument.symbol == "300033.SZ"
    assert isinstance(outcome.strategy.entry, EventCondition)
    assert outcome.strategy.entry.event_code == "event.financial_results.annual_report"
    assert outcome.strategy.entry.document_text is not None
    assert outcome.strategy.entry.document_text.term == term
    assert outcome.strategy.entry.document_text.comparator == comparator
    assert outcome.strategy.entry.document_text.value == value
    assert isinstance(outcome.strategy.exit.children[0], HoldingPeriodExit)
    assert outcome.strategy.exit.children[0].sessions == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    [
        "年报正文AI少于5次买入，买入后3个交易日卖出",
        "年报正文AI不超过5次买入，买入后3个交易日卖出",
        "年报正文AI等于5次买入，买入后3个交易日卖出",
    ],
)
async def test_document_text_upper_bound_or_equality_fails_closed(
    compiler: StrategyCompiler,
    utterance: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300033.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "event_document_metric_not_supported"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phrase", "event_code"),
    [
        ("年度报告", "event.financial_results.annual_report"),
        ("年报", "event.financial_results.annual_report"),
        ("半年度报告", "event.financial_results.semiannual_report"),
        ("半年报", "event.financial_results.semiannual_report"),
        ("中报", "event.financial_results.semiannual_report"),
        ("季度报告", "event.financial_results.quarterly_report"),
        ("季报", "event.financial_results.quarterly_report"),
    ],
)
async def test_periodic_report_names_compile_to_the_exact_event_code(
    compiler: StrategyCompiler,
    phrase: str,
    event_code: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=f"{phrase}发布后买入，3个交易日后卖出",
            instrument_context="300033.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, EventCondition)
    assert outcome.strategy.entry.event_code == event_code


@pytest.mark.asyncio
async def test_explicit_natural_day_exit_gets_one_focused_clarification(
    compiler: StrategyCompiler,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="年报提到AI超过5次买入，3个自然日后卖出",
            instrument_context="300033.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "natural_day_holding_period_requires_clarification"
    assert outcome.clarification is not None
    assert "自然日" in outcome.clarification


@pytest.mark.asyncio
@pytest.mark.parametrize("instrument_context", ["300059.SH", "399001.SZ", "900901.SH"])
async def test_compile_rejects_suffix_conflicts_and_non_share_codes(
    compiler: StrategyCompiler,
    instrument_context: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="MACD 金叉买入，死叉卖出",
            instrument_context=instrument_context,
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "invalid_a_share_instrument"


@pytest.mark.asyncio
async def test_same_inputs_compile_one_hundred_times_to_same_hash(
    compiler: StrategyCompiler,
) -> None:
    request = CompileInput(
        utterance="MACD金叉并且放量2倍买入，死叉卖出",
        instrument_context="300059.SZ",
        as_of_date=date(2026, 8, 27),
    )
    outcomes = [await compiler.compile(request) for _ in range(100)]

    assert {item.status for item in outcomes} == {CompileStatus.READY}
    assert len({item.strategy_hash for item in outcomes}) == 1
    assert isinstance(outcomes[0].strategy.entry, AllCondition)  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_entry_and_or_are_distinct_dsl_and_hashes(compiler: StrategyCompiler) -> None:
    all_outcome = await compiler.compile(
        CompileInput(
            utterance="MACD金叉并且RSI低于30买入，MACD死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )
    any_outcome = await compiler.compile(
        CompileInput(
            utterance="MACD金叉或者RSI低于30买入，MACD死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert all_outcome.status is any_outcome.status is CompileStatus.READY
    assert all_outcome.strategy is not None
    assert any_outcome.strategy is not None
    assert isinstance(all_outcome.strategy.entry, AllCondition)
    assert isinstance(any_outcome.strategy.entry, AnyCondition)
    assert all_outcome.strategy_hash != any_outcome.strategy_hash


@pytest.mark.asyncio
async def test_technical_event_exit_and_or_are_preserved(compiler: StrategyCompiler) -> None:
    all_outcome = await compiler.compile(
        CompileInput(
            utterance="MACD金叉买入，MACD死叉并且重大诉讼卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )
    any_outcome = await compiler.compile(
        CompileInput(
            utterance="MACD金叉买入，MACD死叉或者重大诉讼卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert all_outcome.status is any_outcome.status is CompileStatus.READY
    assert all_outcome.strategy is not None
    assert any_outcome.strategy is not None
    assert len(all_outcome.strategy.exit.children) == 1
    assert isinstance(all_outcome.strategy.exit.children[0], AllCondition)
    assert {type(condition) for condition in all_outcome.strategy.exit.children[0].children} == {
        IndicatorCondition,
        EventCondition,
    }
    assert {type(condition) for condition in any_outcome.strategy.exit.children} == {
        IndicatorCondition,
        EventCondition,
    }
    assert all_outcome.strategy_hash != any_outcome.strategy_hash


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    [
        "MACD金叉并且RSI低于30或者重大诉讼买入，MACD死叉卖出",
        "（MACD金叉或者RSI低于30）并且重大诉讼买入，MACD死叉卖出",
        "MACD金叉RSI低于30买入，MACD死叉卖出",
    ],
)
async def test_mixed_or_nested_boolean_expression_fails_closed(
    compiler: StrategyCompiler,
    utterance: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "ambiguous_boolean_expression"
    assert outcome.strategy is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    [
        "MACD金叉买入，RSI低于30买入，MACD死叉卖出",
        "MACD金叉买入，止盈20%卖出，持有3个交易日卖出",
    ],
)
async def test_repeated_action_clauses_without_a_boolean_connector_fail_closed(
    compiler: StrategyCompiler,
    utterance: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "ambiguous_boolean_expression"
    assert outcome.strategy is None


@pytest.mark.asyncio
async def test_code_can_be_extracted_from_the_sentence(compiler: StrategyCompiler) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="回测300059的MACD金叉买死叉卖",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.instrument.symbol == "300059.SZ"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "instrument_context", "expected_symbol"),
    [
        ("回测430047的MACD金叉买死叉卖", None, "430047.BJ"),
        ("回测920001的MACD金叉买死叉卖", None, "920001.BJ"),
        ("MACD金叉买入，死叉卖出", "830799", "830799.BJ"),
        ("MACD金叉买入，死叉卖出", "920001", "920001.BJ"),
    ],
)
async def test_beijing_stock_codes_stay_inside_the_a_share_boundary(
    compiler: StrategyCompiler,
    utterance: str,
    instrument_context: str | None,
    expected_symbol: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context=instrument_context,
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.instrument.symbol == expected_symbol


@pytest.mark.asyncio
async def test_missing_stock_is_the_only_combined_clarification(
    compiler: StrategyCompiler,
) -> None:
    outcome = await compiler.compile(
        CompileInput(utterance="MACD金叉买死叉卖", as_of_date=date(2026, 8, 27))
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "instrument_required"
    assert outcome.clarification is not None


@pytest.mark.asyncio
async def test_big_drop_rebound_routes_to_unpublished_template_without_question(
    compiler: StrategyCompiler,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="东方财富大跌反弹时买入",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "template_not_published/big_drop_rebound"
    assert outcome.clarification is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    ["业绩预告发布后买入", "MACD金叉买入", "RSI低于30买入"],
)
async def test_entry_without_exit_requires_one_explicit_clarification(
    compiler: StrategyCompiler,
    utterance: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "exit_rule_not_recognized"
    assert outcome.strategy is None
    assert outcome.clarification is not None
    assert "什么条件下卖出" in outcome.clarification
    assert "不会替你补默认卖出规则" in outcome.clarification


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", ["MACD死叉卖出", "年度报告发布后卖出"])
async def test_exit_without_entry_requires_one_symmetric_clarification(
    compiler: StrategyCompiler,
    utterance: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "entry_rule_not_recognized"
    assert outcome.strategy is None
    assert outcome.clarification is not None
    assert "什么条件下买入" in outcome.clarification
    assert "不会替你补默认买入规则" in outcome.clarification


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", ["MACD", "RSI", "年度报告"])
async def test_named_signal_without_buy_or_sell_does_not_invent_a_strategy(
    compiler: StrategyCompiler,
    utterance: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "strategy_rule_incomplete"
    assert outcome.strategy is None
    assert outcome.clarification is not None
    assert "什么时候买入、什么时候卖出" in outcome.clarification
    assert "不会替你生成默认交易策略" in outcome.clarification


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    [
        "MACD买入，MACD卖出",
        "RSI买入，RSI卖出",
        "KDJ买入，KDJ卖出",
        "成交量买入，MACD死叉卖出",
        "OBV买入，MACD死叉卖出",
        "CCI买入，CCI卖出",
        "EMA28买入，EMA28卖出",
        "20日均线买入，20日均线卖出",
        "BBI买入，BBI卖出",
        "EMA28乖离率买入，EMA28乖离率卖出",
    ],
)
async def test_named_indicator_actions_without_triggers_request_one_clarification(
    compiler: StrategyCompiler,
    utterance: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "indicator_trigger_requires_clarification"
    assert outcome.strategy is None
    assert outcome.clarification is not None
    assert "一次写清每个条件" in outcome.clarification
    assert "不会替你补默认触发规则" in outcome.clarification


@pytest.mark.asyncio
async def test_event_entry_and_macd_exit_compile_as_a_mixed_strategy(
    compiler: StrategyCompiler,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="业绩预告发布后买入，MACD死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, EventCondition)
    assert outcome.strategy.entry.event_code.endswith("earnings_forecast_published")
    assert len(outcome.strategy.exit.children) == 1
    exit_condition = outcome.strategy.exit.children[0]
    assert isinstance(exit_condition, IndicatorCondition)
    assert exit_condition.indicator_id == "technical.macd"
    assert exit_condition.trigger == "death_cross"
    assert outcome.strategy.execution.data_capability == "daily_ohlcv_events"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_phrase", "event_code"),
    [
        ("业绩预告", "event.financial_results.earnings_forecast_published"),
        ("业绩快报", "event.financial_results.earnings_flash_report"),
        ("年报", "event.financial_results.annual_report"),
        ("半年报", "event.financial_results.semiannual_report"),
        ("季报", "event.financial_results.quarterly_report"),
    ],
)
async def test_five_periodic_report_entries_preserve_event_lane_and_technical_exit(
    compiler: StrategyCompiler,
    event_phrase: str,
    event_code: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=f"{event_phrase}发布后买入，MACD死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, EventCondition)
    assert outcome.strategy.entry.event_code == event_code
    assert outcome.strategy.entry.attributes == {}
    assert len(outcome.strategy.exit.children) == 1
    exit_condition = outcome.strategy.exit.children[0]
    assert isinstance(exit_condition, IndicatorCondition)
    assert exit_condition.indicator_id == "technical.macd"
    assert exit_condition.trigger == "death_cross"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "diagnostic_code"),
    [
        ("5分钟MACD金叉买入，5分钟死叉卖出", "non_daily_timeframe_not_supported"),
        ("30min MACD金叉买入，30min MACD死叉卖出", "non_daily_timeframe_not_supported"),
        ("周线MACD金叉买入，周线死叉卖出", "non_daily_timeframe_not_supported"),
        ("盘中MACD金叉马上买入，盘中死叉马上卖出", "non_daily_timeframe_not_supported"),
        (
            "MACD金叉当天收盘买入，死叉当天收盘卖出",
            "same_session_execution_not_supported",
        ),
        (
            "MACD金叉后下一交易日收盘买入，死叉后下一交易日收盘卖出",
            "execution_price_time_not_supported",
        ),
        (
            "MACD金叉后第二天收盘买入，MACD死叉卖出",
            "execution_price_time_not_supported",
        ),
        (
            "MACD金叉后第二个交易日收盘买入，MACD死叉卖出",
            "execution_price_time_not_supported",
        ),
        (
            "MACD金叉三天后买入，MACD死叉卖出",
            "execution_price_time_not_supported",
        ),
        (
            "MACD金叉后3个交易日买入，MACD死叉卖出",
            "execution_price_time_not_supported",
        ),
        (
            "MACD金叉买入，MACD死叉三天后卖出",
            "execution_price_time_not_supported",
        ),
        (
            "业绩预告大幅增长后买入，MACD死叉卖出",
            "event_attribute_filter_not_supported",
        ),
        (
            "业绩预告亏损后买入，MACD死叉卖出",
            "event_attribute_filter_not_supported",
        ),
        (
            "业绩快报营收增长30%以上后买入，MACD死叉卖出",
            "event_attribute_filter_not_supported",
        ),
        (
            "2024年报发布后买入，MACD死叉卖出",
            "event_report_period_filter_not_supported",
        ),
        (
            "2024年度报告发布后买入，MACD死叉卖出",
            "event_report_period_filter_not_supported",
        ),
        (
            "今年年报发布后买入，MACD死叉卖出",
            "event_report_period_filter_not_supported",
        ),
        (
            "去年的年度报告发布后买入，MACD死叉卖出",
            "event_report_period_filter_not_supported",
        ),
        (
            "24年报发布后买入，MACD死叉卖出",
            "event_report_period_filter_not_supported",
        ),
        (
            "一季报发布后买入，MACD死叉卖出",
            "event_report_period_filter_not_supported",
        ),
        (
            "MACD在零轴上方金叉买入，MACD死叉卖出",
            "technical_qualifier_not_supported",
        ),
        ("低位MACD金叉买入，MACD死叉卖出", "technical_qualifier_not_supported"),
    ],
)
async def test_unrepresentable_source_semantics_fail_closed_before_strategy_creation(
    compiler: StrategyCompiler,
    utterance: str,
    diagnostic_code: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == diagnostic_code
    assert outcome.strategy is None
    assert outcome.clarification


@pytest.mark.asyncio
async def test_fill_anchored_holding_period_exit_is_not_rejected_as_signal_delay(
    compiler: StrategyCompiler,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="年报发布后买入，持有3个交易日卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.exit.children == (HoldingPeriodExit(sessions=3),)


@pytest.mark.asyncio
async def test_explicit_supported_daily_confirmation_and_next_open_stays_ready(
    compiler: StrategyCompiler,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="日线MACD收盘金叉确认后买入，死叉确认后下一交易日开盘卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.execution.evaluation_frequency == "1d_close"
    assert outcome.strategy.execution.entry_policy == "next_tradable_session_open"
    assert outcome.strategy.execution.exit_policy == "next_tradable_session_open"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phrase", "event_code"),
    [
        ("最终中标", "event.contracts_orders.major_contract_won"),
        ("正式中标", "event.contracts_orders.major_contract_won"),
        ("确定为中标人", "event.contracts_orders.major_contract_won"),
        ("获标", "event.contracts_orders.major_contract_won"),
        ("业务许可获批", "event.macro_policy_industry.license_approval"),
        ("业务许可已明确获批", "event.macro_policy_industry.license_approval"),
        ("许可证已核准", "event.macro_policy_industry.license_approval"),
        ("批件正式获批", "event.macro_policy_industry.license_approval"),
        ("业务资格获得核准", "event.macro_policy_industry.license_approval"),
    ],
)
async def test_narrow_web_event_and_macd_exit_compile(
    compiler: StrategyCompiler,
    phrase: str,
    event_code: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=f"东方财富{phrase}后买入，MACD死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, EventCondition)
    assert outcome.strategy.entry.event_code == event_code
    exit_condition = outcome.strategy.exit.children[0]
    assert isinstance(exit_condition, IndicatorCondition)
    assert exit_condition.trigger == "death_cross"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phrase", "event_code"),
    [
        (
            "现金分红预案",
            "event.dividends_corporate_actions.cash_dividend_proposal",
        ),
        (
            "现金分红方案获股东大会审议通过",
            "event.dividends_corporate_actions.cash_dividend_approved",
        ),
        (
            "现金分红除息",
            "event.dividends_corporate_actions.cash_dividend_ex_date",
        ),
        (
            "资本公积转增股本方案",
            "event.dividends_corporate_actions.capitalization_issue",
        ),
        ("配股发行方案", "event.dividends_corporate_actions.rights_issue"),
        ("送股除权", "event.dividends_corporate_actions.stock_dividend_ex_date"),
        ("送股预案", "event.dividends_corporate_actions.stock_dividend_proposal"),
        (
            "限售股份上市流通",
            "event.restricted_shares_pledges.restricted_shares_unlock",
        ),
        (
            "变更限售股解禁安排",
            "event.restricted_shares_pledges.unlock_schedule_change",
        ),
        (
            "实际控制人发生变更",
            "event.shareholder_holdings.actual_controller_change",
        ),
        ("董事减持股份实施完成", "event.shareholder_holdings.executive_decrease"),
        (
            "高级管理人员增持股份实施完成",
            "event.shareholder_holdings.executive_increase",
        ),
        (
            "持股比例降至5%以下",
            "event.shareholder_holdings.ownership_below_five_percent",
        ),
        (
            "持股比例达到5%",
            "event.shareholder_holdings.ownership_reaches_five_percent",
        ),
        (
            "控股股东减持计划",
            "event.shareholder_holdings.major_holder_decrease_plan",
        ),
        (
            "控股股东减持计划进展",
            "event.shareholder_holdings.major_holder_decrease_progress",
        ),
        (
            "控股股东增持计划",
            "event.shareholder_holdings.major_holder_increase_plan",
        ),
        (
            "控股股东增持计划完成",
            "event.shareholder_holdings.major_holder_increase_progress",
        ),
        ("回购股份方案", "event.repurchase_capital.repurchase_proposal"),
        (
            "回购股份方案获股东大会批准",
            "event.repurchase_capital.repurchase_approved",
        ),
        (
            "首次回购公司股份",
            "event.repurchase_capital.repurchase_first_execution",
        ),
        ("回购股份进展", "event.repurchase_capital.repurchase_progress"),
        ("回购股份实施完成", "event.repurchase_capital.repurchase_completion"),
        ("终止回购股份", "event.repurchase_capital.repurchase_termination"),
        ("完成回购股份注销", "event.repurchase_capital.repurchase_cancellation"),
        ("变更回购股份方案", "event.repurchase_capital.repurchase_change"),
        ("回购方案变更", "event.repurchase_capital.repurchase_change"),
        (
            "限制性股票激励计划方案",
            "event.governance_personnel.equity_incentive_plan",
        ),
        (
            "限制性股票激励计划首次授予",
            "event.governance_personnel.equity_incentive_grant",
        ),
        ("收到行政处罚决定书", "event.regulation_risk.administrative_penalty"),
        ("收到立案告知书", "event.regulation_risk.investigation_opened"),
        (
            "信息披露违规认定",
            "event.regulation_risk.information_disclosure_violation",
        ),
        ("收到纪律处分决定", "event.regulation_risk.disciplinary_action"),
        ("收到公开谴责决定", "event.regulation_risk.public_censure"),
        ("新增重大诉讼", "event.litigation_credit.major_litigation"),
        ("重大诉讼进展", "event.litigation_credit.litigation_progress"),
        ("新增重大仲裁", "event.litigation_credit.arbitration"),
        ("债券违约", "event.litigation_credit.debt_default"),
        ("债券未能按期兑付", "event.litigation_credit.debt_default"),
        ("主体信用评级下调", "event.litigation_credit.credit_rating_downgrade"),
        (
            "法院受理公司破产重整申请",
            "event.litigation_credit.bankruptcy_reorganization",
        ),
        ("董事长辞职", "event.governance_personnel.chairman_change"),
        ("聘任公司总经理", "event.governance_personnel.ceo_change"),
        ("财务负责人辞职", "event.governance_personnel.cfo_change"),
        ("董事会秘书变更", "event.governance_personnel.board_secretary_change"),
        ("公司董事辞职", "event.governance_personnel.director_resignation"),
        ("公司监事辞职", "event.governance_personnel.supervisor_resignation"),
        (
            "终止重大资产重组",
            "event.m_and_a_restructuring.restructuring_terminated",
        ),
        (
            "重大资产重组实施完成",
            "event.m_and_a_restructuring.restructuring_completed",
        ),
        (
            "发行股份购买资产获得证监会同意注册",
            "event.m_and_a_restructuring.restructuring_regulatory_approval",
        ),
        (
            "股东大会审议通过发行股份购买资产",
            "event.m_and_a_restructuring.restructuring_approved_shareholders",
        ),
        (
            "董事会审议通过发行股份购买资产",
            "event.m_and_a_restructuring.restructuring_approved_board",
        ),
        ("重大资产重组复牌", "event.m_and_a_restructuring.restructuring_resumption"),
        ("重大资产重组停牌", "event.m_and_a_restructuring.restructuring_suspension"),
        ("分拆子公司上市", "event.m_and_a_restructuring.spin_off_listing"),
        ("重大资产出售预案", "event.m_and_a_restructuring.asset_sale_plan"),
        ("发行股份购买资产预案", "event.m_and_a_restructuring.acquisition_plan"),
        ("终止重大合同", "event.contracts_orders.contract_terminated"),
        ("重大合同变更", "event.contracts_orders.contract_changed"),
        ("重大合同履行完毕", "event.contracts_orders.contract_completed"),
        ("重大合同履行进展", "event.contracts_orders.contract_progress"),
        ("重大项目中标", "event.contracts_orders.major_contract_won"),
        ("签订重大经营合同", "event.contracts_orders.major_contract_signed"),
        ("签署战略合作框架协议", "event.contracts_orders.framework_agreement"),
        ("收到重大订单", "event.contracts_orders.purchase_order_received"),
    ],
)
async def test_explicit_announcement_stage_and_macd_exit_compile(
    compiler: StrategyCompiler,
    phrase: str,
    event_code: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=f"东方财富{phrase}后买入，MACD死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, EventCondition)
    assert outcome.strategy.entry.event_code == event_code
    assert outcome.strategy.execution.data_capability == "daily_ohlcv_events"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    [
        "回购公告",
        "利润分配",
        "重组",
        "公告",
        "重大合同公告",
        "收到行政处罚事先告知书",
        "副总经理辞职",
        "破产重整意向",
        "未获得证监会同意注册",
        "股东大会未审议通过发行股份购买资产",
        "未完成回购股份",
        "不终止回购股份",
        "总经理辞职传闻不实的澄清",
        "公司不存在债务违约",
    ],
)
async def test_announcement_without_explicit_stage_fails_closed(
    compiler: StrategyCompiler,
    phrase: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=f"东方财富{phrase}后买入，MACD死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "event_not_executable"


def test_announcement_parser_codes_match_the_executable_registry() -> None:
    announcement_codes = {event_code for _, event_code in _ANNOUNCEMENT_EVENT_DEFINITIONS}
    fixed_parser_codes = {
        "event.financial_results.earnings_forecast_published",
        "event.financial_results.earnings_flash_report",
        "event.financial_results.annual_report",
        "event.financial_results.semiannual_report",
        "event.financial_results.quarterly_report",
        "event.contracts_orders.major_contract_won",
        "event.macro_policy_industry.license_approval",
    }

    assert announcement_codes <= set(EXECUTABLE_EVENT_DEFINITIONS)
    assert announcement_codes | fixed_parser_codes == set(EXECUTABLE_EVENT_DEFINITIONS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase",
    [
        "中标",
        "中标候选人",
        "预中标",
        "入围",
        "未最终中标",
        "尚未正式中标",
        "未获标",
        "中标失败",
        "获批",
        "许可证申请已受理",
        "业务许可待批",
        "批件审批中",
        "业务资格pending",
    ],
)
async def test_ambiguous_or_non_final_web_event_is_not_executable(
    compiler: StrategyCompiler,
    phrase: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=f"东方财富{phrase}后买入，MACD死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "event_not_executable"


@pytest.mark.asyncio
async def test_user_text_is_data_and_is_never_executed(compiler: StrategyCompiler) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="__import__('os').system('touch /tmp/should-not-exist')",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "no_supported_signal_recognized"


@pytest.mark.asyncio
async def test_rsi_defaults_are_explicitly_provenanced(compiler: StrategyCompiler) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="RSI超卖就买，超买就卖",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    default_paths = {
        item.path for item in outcome.provenance if item.source == "default/catalog_policy"
    }
    assert "/entry/rsi/value" in default_paths
    assert "/exit/rsi/value" in default_paths
    assert outcome.strategy is not None
    assert outcome.strategy.entry.trigger == "below"  # type: ignore[union-attr]
    assert outcome.strategy.exit.children[0].trigger == "above"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_rsi_level_words_do_not_silently_compile_as_crossovers(
    compiler: StrategyCompiler,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="300059 RSI低于30买入，高于70卖出",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.entry.trigger == "below"  # type: ignore[union-attr]
    assert outcome.strategy.exit.children[0].trigger == "above"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_rsi_crossover_words_compile_as_one_shot_crossovers(
    compiler: StrategyCompiler,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="300059 RSI跌破30买入，突破70卖出",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.entry.trigger == "crosses_below"  # type: ignore[union-attr]
    assert outcome.strategy.exit.children[0].trigger == "crosses_above"  # type: ignore[union-attr]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "indicator_id", "entry_trigger", "exit_trigger"),
    [
        (
            "股价上穿EMA28买入，跌破EMA28卖出",
            "technical.ema",
            "price_crosses_above",
            "price_crosses_below",
        ),
        (
            "5日均线上穿20日均线买入，5日均线下穿20日均线卖出",
            "technical.ma_cross",
            "golden_cross",
            "death_cross",
        ),
        ("KDJ金叉买入，死叉卖出", "technical.kdj", "golden_cross", "death_cross"),
        ("CCI低于-100买入，高于100卖出", "technical.cci", "below", "above"),
        (
            "股价突破布林上轨买入，跌破布林中轨卖出",
            "technical.bollinger",
            "price_crosses_above_upper",
            "price_crosses_below_middle",
        ),
        (
            "股价上穿BBI买入，跌破BBI卖出",
            "technical.bbi",
            "price_crosses_above",
            "price_crosses_below",
        ),
        (
            "EMA28乖离率低于-5%买入，高于5%卖出",
            "technical.ema_bias",
            "below",
            "above",
        ),
        (
            "放量上涨5%买入，放量下跌5%卖出",
            "volume.price_confirmation",
            "surge_up",
            "surge_down",
        ),
    ],
)
async def test_first_indicator_expansion_golden_sentences_compile_by_clause(
    compiler: StrategyCompiler,
    utterance: str,
    indicator_id: str,
    entry_trigger: str,
    exit_trigger: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == indicator_id
    assert outcome.strategy.entry.trigger == entry_trigger
    assert len(outcome.strategy.exit.children) == 1
    exit_condition = outcome.strategy.exit.children[0]
    assert isinstance(exit_condition, IndicatorCondition)
    assert exit_condition.indicator_id == indicator_id
    assert exit_condition.trigger == exit_trigger


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "indicator_id", "entry_trigger", "exit_trigger"),
    [
        (
            "MACD DIF下穿DEA买入，DIF上穿DEA卖出",
            "technical.macd",
            "death_cross",
            "golden_cross",
        ),
        (
            "KDJ K线下穿D线买入，KDJ K线上穿D线卖出",
            "technical.kdj",
            "death_cross",
            "golden_cross",
        ),
        (
            "MACD上穿零轴买入，MACD下穿零轴卖出",
            "technical.macd",
            "crosses_above_zero",
            "crosses_below_zero",
        ),
    ],
)
async def test_explicit_indicator_line_direction_is_not_replaced_by_action_defaults(
    compiler: StrategyCompiler,
    utterance: str,
    indicator_id: str,
    entry_trigger: str,
    exit_trigger: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == indicator_id
    assert outcome.strategy.entry.trigger == entry_trigger
    exit_condition = outcome.strategy.exit.children[0]
    assert isinstance(exit_condition, IndicatorCondition)
    assert exit_condition.indicator_id == indicator_id
    assert exit_condition.trigger == exit_trigger


@pytest.mark.asyncio
async def test_trend_weakening_exits_when_uptrend_is_lost(compiler: StrategyCompiler) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="阶段上涨趋势买入，趋势转弱卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.trigger == "uptrend"
    assert {
        condition.trigger
        for condition in outcome.strategy.exit.children
        if isinstance(condition, IndicatorCondition)
    } == {"range", "downtrend"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "diagnostic_code"),
    [
        ("MACD零轴买入，MACD死叉卖出", "ambiguous_macd_trigger"),
        ("KDJ K线上穿买入，KDJ死叉卖出", "ambiguous_kdj_trigger"),
    ],
)
async def test_incomplete_indicator_cross_language_fails_closed(
    compiler: StrategyCompiler,
    utterance: str,
    diagnostic_code: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == diagnostic_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "indicator_id", "trigger", "value", "params"),
    [
        (
            "5日跌幅超过10%买入，MACD死叉卖出",
            "price.return_pct",
            "below",
            -10,
            {"period": 5, "price_field": "close"},
        ),
        (
            "股价在5日均线上方买入，跌破5日均线卖出",
            "technical.ma",
            "price_above",
            None,
            {"period": 5, "price_field": "close"},
        ),
        (
            "5日均线高于20日均线买入，5日均线低于20日均线卖出",
            "technical.ma_cross",
            "fast_above_slow",
            None,
            {"fast_period": 5, "slow_period": 20, "price_field": "close"},
        ),
    ],
)
async def test_direction_and_level_language_preserves_its_mathematical_meaning(
    compiler: StrategyCompiler,
    utterance: str,
    indicator_id: str,
    trigger: str,
    value: float | None,
    params: dict[str, str | int | float | bool],
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == indicator_id
    assert outcome.strategy.entry.trigger == trigger
    assert outcome.strategy.entry.value == value
    assert outcome.strategy.entry.params == params


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "indicator_id", "expected_params"),
    [
        (
            "MACD(6,13,5)金叉买入，死叉卖出",
            "technical.macd",
            {"fast": 6, "slow": 13, "signal": 5},
        ),
        ("RSI6低于30买入，高于70卖出", "technical.rsi", {"period": 6}),
        (
            "KDJ(14,3,3)金叉买入，死叉卖出",
            "technical.kdj",
            {"period": 14, "k_smoothing": 3, "d_smoothing": 3},
        ),
    ],
)
async def test_explicit_indicator_parameters_are_preserved(
    compiler: StrategyCompiler,
    utterance: str,
    indicator_id: str,
    expected_params: dict[str, int],
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == indicator_id
    assert outcome.strategy.entry.params == expected_params


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "diagnostic_code"),
    [
        ("股价低于20日最高价买入，MACD死叉卖出", "no_supported_signal_recognized"),
        ("RSI不低于30买入，MACD死叉卖出", "inclusive_comparator_not_supported"),
        ("不要在MACD金叉时买入，MACD死叉卖出", "negated_signal_not_supported"),
        ("MACD(6,13)金叉买入，死叉卖出", "unsupported_macd_parameters"),
        ("RSI至少30买入，MACD死叉卖出", "unsupported_comparator"),
        ("RSI等于30买入，MACD死叉卖出", "unsupported_comparator"),
        ("RSI达到30买入，MACD死叉卖出", "unsupported_comparator"),
        ("RSI在30到40之间买入，MACD死叉卖出", "unsupported_comparator"),
        ("RSI≤30买入，MACD死叉卖出", "unsupported_comparator"),
        ("CCI等于-100买入，MACD死叉卖出", "unsupported_comparator"),
        ("EMA28乖离率等于-5买入，MACD死叉卖出", "unsupported_comparator"),
        ("RSI上涨买入，MACD死叉卖出", "unsupported_rsi_direction"),
        ("CCI上涨买入，MACD死叉卖出", "unsupported_cci_direction"),
        ("EMA28向上买入，MACD死叉卖出", "unsupported_ema_direction"),
        ("KDJ超买时买入，MACD死叉卖出", "unsupported_kdj_overbought_semantics"),
        ("20日均线向上买入，MACD死叉卖出", "unsupported_ma_direction"),
        ("BBI向上买入，MACD死叉卖出", "unsupported_bbi_direction"),
        ("OBV不下降买入，MACD死叉卖出", "negated_signal_not_supported"),
        ("不放量上涨买入，MACD死叉卖出", "negated_signal_not_supported"),
        (
            "连续上涨少于3天买入，MACD死叉卖出",
            "unsupported_consecutive_up_comparator",
        ),
    ],
)
async def test_unsupported_semantics_fail_closed_instead_of_reversing_meaning(
    compiler: StrategyCompiler,
    utterance: str,
    diagnostic_code: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == diagnostic_code


@pytest.mark.asyncio
async def test_consecutive_up_comparators_preserve_clause_local_thresholds(
    compiler: StrategyCompiler,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="连续上涨超过3天买入，超过5天卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == "price.consecutive_up"
    assert outcome.strategy.entry.params == {"days": 4}
    exit_condition = outcome.strategy.exit.children[0]
    assert isinstance(exit_condition, IndicatorCondition)
    assert exit_condition.indicator_id == "price.consecutive_up"
    assert exit_condition.params == {"days": 6}


@pytest.mark.asyncio
async def test_indicator_defaults_do_not_leak_across_buy_and_sell_clauses(
    compiler: StrategyCompiler,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="RSI低于30买入，MACD死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == "technical.rsi"
    assert len(outcome.strategy.exit.children) == 1
    exit_condition = outcome.strategy.exit.children[0]
    assert isinstance(exit_condition, IndicatorCondition)
    assert exit_condition.indicator_id == "technical.macd"


@pytest.mark.asyncio
async def test_dual_moving_average_is_not_misread_as_price_crossing_one_average(
    compiler: StrategyCompiler,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="5日线上穿20日线买入，5日线下穿20日线卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == "technical.ma_cross"
    assert outcome.strategy.entry.params == {
        "fast_period": 5,
        "slow_period": 20,
        "price_field": "close",
    }


@pytest.mark.asyncio
async def test_bare_cross_requires_indicator_disambiguation(compiler: StrategyCompiler) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="金叉买入，死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "ambiguous_cross_indicator"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "indicator_id", "trigger", "value", "params"),
    [
        (
            "5日涨幅超过10%买入，MACD死叉卖出",
            "price.return_pct",
            "above",
            10,
            {"period": 5, "price_field": "close"},
        ),
        (
            "股价创20日新高买入，MACD死叉卖出",
            "price.rolling_high",
            "new_high",
            None,
            {"period": 20, "price_field": "close"},
        ),
        (
            "连续上涨3天买入，MACD死叉卖出",
            "price.consecutive_up",
            "at_least",
            None,
            {"days": 3},
        ),
        (
            "振幅超过5%买入，MACD死叉卖出",
            "price.amplitude",
            "above",
            5,
            {},
        ),
        (
            "成交额突破1亿元买入，MACD死叉卖出",
            "market.amount",
            "crosses_above",
            100_000_000,
            {},
        ),
        (
            "5日平均成交额超过1亿元买入，MACD死叉卖出",
            "amount.average",
            "above",
            100_000_000,
            {"period": 5},
        ),
    ],
)
async def test_second_indicator_expansion_parses_percentages_and_amount_units(
    compiler: StrategyCompiler,
    utterance: str,
    indicator_id: str,
    trigger: str,
    value: float | None,
    params: dict[str, str | int | float | bool],
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == indicator_id
    assert outcome.strategy.entry.trigger == trigger
    assert outcome.strategy.entry.value == value
    assert outcome.strategy.entry.params == params
    assert len(outcome.strategy.exit.children) == 1
    exit_condition = outcome.strategy.exit.children[0]
    assert isinstance(exit_condition, IndicatorCondition)
    assert exit_condition.indicator_id == "technical.macd"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "indicator_id", "trigger", "value"),
    [
        ("放量1.5倍买入，MACD死叉卖出", "volume.relative", "gte_multiple", 1.5),
        (
            "连续3日放量买入，MACD死叉卖出",
            "volume.relative",
            "consecutive_gte_multiple",
            1.2,
        ),
        (
            "放量大涨5%买入，MACD死叉卖出",
            "volume.price_confirmation",
            "surge_up",
            None,
        ),
        ("量价底背离买入，MACD死叉卖出", "volume.price_divergence", "bullish", None),
        ("OBV上升买入，MACD死叉卖出", "technical.obv", "rising", None),
        ("阶段上涨趋势买入，MACD死叉卖出", "technical.trend_regime", "uptrend", None),
    ],
)
async def test_volume_and_trend_sentences_compile_to_versioned_indicators(
    compiler: StrategyCompiler,
    utterance: str,
    indicator_id: str,
    trigger: str,
    value: float | None,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == indicator_id
    assert outcome.strategy.entry.trigger == trigger
    assert outcome.strategy.entry.value == value


@pytest.mark.asyncio
async def test_obv_direction_is_inherited_by_the_sell_clause(
    compiler: StrategyCompiler,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="OBV上升买入，下降卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == "technical.obv"
    assert outcome.strategy.entry.trigger == "rising"
    exit_condition = outcome.strategy.exit.children[0]
    assert isinstance(exit_condition, IndicatorCondition)
    assert exit_condition.indicator_id == "technical.obv"
    assert exit_condition.trigger == "falling"


@pytest.mark.asyncio
async def test_volume_price_direction_and_threshold_are_preserved_by_clause(
    compiler: StrategyCompiler,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="放量上涨5%买入，放量下跌5%卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == "volume.price_confirmation"
    assert outcome.strategy.entry.trigger == "surge_up"
    assert outcome.strategy.entry.params == {
        "baseline_period": 20,
        "return_threshold_pct": 5.0,
        "volume_multiple": 1.5,
    }
    exit_condition = outcome.strategy.exit.children[0]
    assert isinstance(exit_condition, IndicatorCondition)
    assert exit_condition.indicator_id == "volume.price_confirmation"
    assert exit_condition.trigger == "surge_down"
    assert exit_condition.params == outcome.strategy.entry.params


@pytest.mark.asyncio
async def test_energy_tide_is_an_obv_alias(compiler: StrategyCompiler) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance="能量潮上升买入，下降卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == "technical.obv"
    assert outcome.strategy.entry.trigger == "rising"
    exit_condition = outcome.strategy.exit.children[0]
    assert isinstance(exit_condition, IndicatorCondition)
    assert exit_condition.indicator_id == "technical.obv"
    assert exit_condition.trigger == "falling"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "indicator_id", "entry_trigger", "exit_trigger"),
    [
        (
            "量价底背离买入，顶背离卖出",
            "volume.price_divergence",
            "bullish",
            "bearish",
        ),
        (
            "阶段上涨趋势买入，转为下跌卖出",
            "technical.trend_regime",
            "uptrend",
            "downtrend",
        ),
        (
            "趋势向上买入，向下卖出",
            "technical.trend_regime",
            "uptrend",
            "downtrend",
        ),
        (
            "量价齐升买入，齐跌卖出",
            "volume.price_confirmation",
            "surge_up",
            "surge_down",
        ),
    ],
)
async def test_volume_and_trend_family_is_inherited_by_short_sell_clause(
    compiler: StrategyCompiler,
    utterance: str,
    indicator_id: str,
    entry_trigger: str,
    exit_trigger: str,
) -> None:
    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 27),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == indicator_id
    assert outcome.strategy.entry.trigger == entry_trigger
    exit_condition = outcome.strategy.exit.children[0]
    assert isinstance(exit_condition, IndicatorCondition)
    assert exit_condition.indicator_id == indicator_id
    assert exit_condition.trigger == exit_trigger
