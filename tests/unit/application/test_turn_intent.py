import pytest

from ashare_lab.application.turn_intent import (
    TurnIntent,
    classify_clarification_turn,
    continues_prior_viewpoint,
    data_query_needs_instrument_context,
    data_query_unsupported_scope,
    data_query_uses_screening,
    extract_change_instrument_target,
    extract_data_query_indicators,
    extract_screened_finance_indicators,
    extract_screening_query,
)


@pytest.mark.parametrize("utterance", ["1", "一", "第一个", "选择第二项", "我选第三条", "用１", "第２个"])
def test_ordinal_choice_requires_visible_options(utterance):
    assert classify_clarification_turn(utterance, has_options=True) is TurnIntent.SELECT_OPTION
    assert classify_clarification_turn(utterance, has_options=False) is TurnIntent.UNKNOWN


@pytest.mark.parametrize(
    ("utterance", "target"),
    [
        ("改成同花顺这个", "同花顺"),
        ("换成300033.SZ", "300033.SZ"),
    ],
)
def test_explicit_instrument_change_has_its_own_intent(
    utterance: str,
    target: str,
) -> None:
    assert extract_change_instrument_target(utterance) == target
    assert classify_clarification_turn(utterance) is TurnIntent.CHANGE_INSTRUMENT


def test_complete_rule_replaces_pending_clarification() -> None:
    assert classify_clarification_turn("东方财富上涨5%卖出，下跌2%买入") is TurnIntent.NEW_STRATEGY


@pytest.mark.parametrize(
    "utterance",
    [
        "MACD金叉买入，不卖出",
        "MACD金叉买入，暂不卖出",
        "MACD金叉买入，没有卖出",
    ],
)
def test_explicitly_missing_exit_is_not_a_complete_buy_sell_rule(utterance: str) -> None:
    assert classify_clarification_turn(utterance) is TurnIntent.SUPPLEMENT


def test_vague_but_inferable_rule_is_not_casual() -> None:
    assert classify_clarification_turn("低买高卖") is TurnIntent.VAGUE_STRATEGY


def test_viewpoint_routes_to_strategy_guidance() -> None:
    assert classify_clarification_turn("我看好东方财富后面会涨") is TurnIntent.VIEWPOINT
    assert classify_clarification_turn("我觉得东方财富会涨") is TurnIntent.VIEWPOINT
    assert classify_clarification_turn("我讨厌特朗普") is TurnIntent.VIEWPOINT


@pytest.mark.parametrize("utterance", ["我讨厌wash", "而且我讨厌wash"])
def test_ambiguous_latin_preference_does_not_invent_a_market_viewpoint(
    utterance: str,
) -> None:
    assert classify_clarification_turn(utterance) is TurnIntent.CASUAL
    assert not continues_prior_viewpoint(utterance)


def test_casual_sentence_does_not_pollute_pending_strategy() -> None:
    assert classify_clarification_turn("你好，我是你爸") is TurnIntent.CASUAL


@pytest.mark.parametrize(
    "utterance",
    ["今天天气怎么样", "你好，今天会下雨吗", "晚上吃什么", "现在几点了"],
)
def test_obvious_lifestyle_question_is_casual_not_viewpoint(utterance: str) -> None:
    assert classify_clarification_turn(utterance) is TurnIntent.CASUAL


def test_one_sided_rule_remains_a_supplement() -> None:
    assert classify_clarification_turn("MACD死叉卖出") is TurnIntent.SUPPLEMENT


@pytest.mark.parametrize(
    "utterance",
    [
        "买入条件改为：收盘价创前80日新高。其他条件和回测设置保持不变。",
        "买入条件改为：收盘价创近80日新高。其他条件和回测设置保持不变。",
        "卖出规则调整为近20日最高价回撤5%",
        "请把买入信号换成近20日成交额放大2倍",
    ],
)
def test_explicit_condition_edits_reach_the_model_editor(utterance: str) -> None:
    assert classify_clarification_turn(utterance) is TurnIntent.SUPPLEMENT


@pytest.mark.parametrize("period", ["80日", "20个交易日", "8周", "6个月", "3年", "5分钟"])
def test_prior_time_windows_are_not_screening_ranks(period: str) -> None:
    utterance = f"收盘价创前{period}新高买入"
    assert not data_query_uses_screening(utterance)
    assert classify_clarification_turn(utterance) is TurnIntent.SUPPLEMENT


@pytest.mark.parametrize(
    ("utterance", "screening"),
    [
        ("涨幅前80", True),
        ("涨幅前5只股票", True),
        ("ETF成交额从高到低取前3", True),
        ("查询东方财富近80日收盘价", False),
        ("东方财富换手率多少", False),
    ],
)
def test_actual_rankings_and_stock_lookups_remain_data_queries(
    utterance: str, screening: bool,
) -> None:
    assert classify_clarification_turn(utterance) is TurnIntent.DATA_QUERY
    assert data_query_uses_screening(utterance) is screening


def test_plain_name_is_left_for_authoritative_resolver() -> None:
    assert classify_clarification_turn("同花顺") is TurnIntent.UNKNOWN


def test_ordinal_is_a_choice_only_when_server_options_exist() -> None:
    assert classify_clarification_turn("1", has_options=True) is TurnIntent.SELECT_OPTION
    assert classify_clarification_turn("选二", has_options=True) is TurnIntent.SELECT_OPTION
    assert classify_clarification_turn("1", has_options=False) is TurnIntent.UNKNOWN


@pytest.mark.parametrize(
    "utterance",
    [
        "同花顺两年前的今天的换手率是多少",
        "东方财富昨天的换手率是多少",
        "东方财富换手率怎么样",
        "A股上涨股票怎么样",
        "上涨的股票；获取近10年的归母净利润",
    ],
)
def test_current_data_questions_are_routed_before_strategy_supplements(
    utterance: str,
) -> None:
    assert classify_clarification_turn(utterance) is TurnIntent.DATA_QUERY


def test_plain_company_opinion_still_routes_to_viewpoint() -> None:
    assert classify_clarification_turn("同花顺咋样") is TurnIntent.VIEWPOINT
    assert classify_clarification_turn("我看好东方财富") is TurnIntent.VIEWPOINT


@pytest.mark.parametrize("utterance", ["东方财富换手率", "同花顺PE", "它的最新市盈率"])
def test_short_metric_lookup_without_question_word_is_still_a_data_query(
    utterance: str,
) -> None:
    assert classify_clarification_turn(utterance) is TurnIntent.DATA_QUERY


@pytest.mark.parametrize(
    "utterance",
    [
        "东方财富股东户数是多少",
        "同花顺两融余额多少",
        "查询300059.SZ机构持仓家数",
        "告诉我东方财富最新的融资融券余额",
    ],
)
def test_provider_owned_metric_aliases_route_to_live_data_without_local_catalog(
    utterance: str,
) -> None:
    assert classify_clarification_turn(utterance) is TurnIntent.DATA_QUERY


@pytest.mark.parametrize(
    "utterance",
    [
        "现在几点",
        "今天天气怎么样",
        "wash是谁",
        "我讨厌特朗普",
    ],
)
def test_general_questions_do_not_leak_into_finance_data_route(utterance: str) -> None:
    assert classify_clarification_turn(utterance) is not TurnIntent.DATA_QUERY


@pytest.mark.parametrize(
    "utterance",
    [
        "昨天换手率多少",
        "请帮我查昨天换手率",
        "它的最新市盈率",
        "换手率多少",
    ],
)
def test_entity_omitted_data_query_can_reuse_verified_instrument_context(
    utterance: str,
) -> None:
    assert data_query_needs_instrument_context(utterance)


@pytest.mark.parametrize(
    "utterance",
    [
        "东方财富换手率",
        "同花顺PE",
        "查询300059.SZ昨天换手率",
        "市盈率低于20买入",
    ],
)
def test_explicit_entity_or_strategy_does_not_absorb_old_instrument_context(
    utterance: str,
) -> None:
    assert not data_query_needs_instrument_context(utterance)


def test_only_discovery_queries_use_the_screener() -> None:
    assert data_query_uses_screening("上涨的股票；获取近10年的归母净利润")
    assert not data_query_uses_screening("东方财富昨天的换手率是多少")


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("上涨的股票；获取近10年的归母净利润", "近10年的归母净利润"),
        ("筛选A股涨幅前5，再查询最新市盈率", "最新市盈率"),
        ("A股近一年涨幅前5只股票", None),
        ("ETF成交额从高到低取前3", None),
    ],
)
def test_screen_followup_metric_requires_an_explicit_second_request(
    utterance: str,
    expected: str | None,
) -> None:
    assert extract_screened_finance_indicators(utterance) == expected


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("上涨的股票；获取近10年的归母净利润", "上涨的股票"),
        ("A股今日上涨的股票；获取最新价和换手率", "A股今日上涨的股票"),
        ("筛选A股涨幅前5，再查询最新市盈率", "筛选A股涨幅前5"),
        ("ETF成交额从高到低取前3", "ETF成交额从高到低取前3"),
    ],
)
def test_composed_query_keeps_finance_request_out_of_screener(
    utterance: str,
    expected: str,
) -> None:
    assert extract_screening_query(utterance) == expected


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("查询东方财富当前市盈率", "当前市盈率"),
        ("同花顺两年前的今天的换手率是多少？", "两年前的今天的换手率"),
        ("东方财富昨天的换手率是多少", "昨天的换手率"),
    ],
)
def test_indicator_hint_keeps_user_wording_but_excludes_the_entity(
    utterance: str,
    expected: str,
) -> None:
    assert extract_data_query_indicators(utterance) == expected


def test_strategy_language_still_wins_over_data_routing() -> None:
    assert classify_clarification_turn("东方财富上涨5%卖出，下跌2%买入") is TurnIntent.NEW_STRATEGY
    assert classify_clarification_turn("市盈率低于20买入") is TurnIntent.SUPPLEMENT


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("港股涨幅前5只股票", "港股"),
        ("纳斯达克市值前10只股票", "美股"),
        ("普通混合型基金近一年收益前5", "普通基金"),
        ("ETF成交额从高到低取前3", None),
        ("A股近一年涨幅前5只股票", None),
    ],
)
def test_explicit_unsupported_market_never_silently_defaults_to_a_share(
    utterance: str,
    expected: str | None,
) -> None:
    assert data_query_unsupported_scope(utterance) == expected
