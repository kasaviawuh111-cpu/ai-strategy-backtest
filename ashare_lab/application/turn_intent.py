"""Small, deterministic router for one clarification turn.

The router does not parse strategies and never grants execution authority.  It
only decides whether the next sentence should replace the pending sentence or
be merged into it.  The existing compiler remains the sole DSL gate.
"""

from __future__ import annotations

import re
from enum import StrEnum


class TurnIntent(StrEnum):
    SELECT_OPTION = "select_option"
    NEW_STRATEGY = "new_strategy"
    VAGUE_STRATEGY = "vague_strategy"
    SUPPLEMENT = "supplement"
    VIEWPOINT = "viewpoint"
    DATA_QUERY = "data_query"
    CASUAL = "casual"
    SAFETY = "safety"
    CANCEL = "cancel"
    CHANGE_INSTRUMENT = "change_instrument"
    UNKNOWN = "unknown"


_BUY_RE = re.compile(r"(?:买入|买进|买|建仓|开仓|低吸|抄底)", re.IGNORECASE)
# A high-priority safety guard, not a general semantic classifier. Ordinary
# conversation is still understood by the dialogue model, not keyword templates.
_SAFETY_RE = re.compile(
    r"(?:我(?:真的|现在|已经|快要|很)?(?:想死|不想活|想自杀|要自杀|想伤害自己)|"
    r"(?:想|准备|打算)结束自己的生命|活不下去了)",
)
_SELL_RE = re.compile(
    r"(?:卖出|卖掉|卖|退出|平仓|止盈|止损|高抛)",
    re.IGNORECASE,
)
_NEGATED_SELL_RE = re.compile(
    r"(?:暂(?:时|且)?|先)?\s*不(?:再|考虑|打算)?\s*"
    r"(?:卖出|卖掉|卖|退出|平仓|止盈|止损)|"
    r"(?:没有|没(?:有)?|尚未|还没)\s*"
    r"(?:明确(?:的)?|设置|写|给出)?\s*"
    r"(?:卖出|卖掉|退出|平仓|止盈|止损)(?:条件|规则|计划)?",
    re.IGNORECASE,
)
_VAGUE_STRATEGY_RE = re.compile(
    r"(?:低买高卖|高抛低吸|低吸高抛|做短线|做波段|抄底|超跌反弹)",
    re.IGNORECASE,
)
_CONTINUATION_PREFIX_RE = re.compile(
    r"^(?:而且|并且|另外|还有|同时|再加上|顺便|除此之外|"
    r"不过|但是|以及)[，,：:\s]*",
    re.IGNORECASE,
)
_VIEWPOINT_RE = re.compile(
    r"(?:看好|看多|看空|不看好|觉得.+(?:涨|跌)|后面会?(?:涨|跌)|怎么样|咋样|怎么看|"
    r"给我(?:来)?(?:两|二|三|\d+)?个?策略|什么策略|"
    r"(?:我|本人|咱们?)[^，。；;!！?？]{0,8}(?:喜欢|讨厌|支持|反对)"
    r"[^，。；;!！?？]{1,32})",
    re.IGNORECASE,
)
_AMBIGUOUS_LATIN_PREFERENCE_RE = re.compile(
    r"^(?:(?:而且|并且|另外|还有|同时|再加上|顺便|不过|但是)[，,：:\s]*)?"
    r"(?:我|本人|咱们?)(?:真的|很|特别|非常)?(?:喜欢|讨厌|支持|反对)\s*"
    r"[A-Za-z][A-Za-z0-9_.\-]{1,31}[。.!！?？]*$",
    re.IGNORECASE,
)
_SCREEN_QUERY_RE = re.compile(
    r"(?:筛选|选出|找出|哪些|哪几|排名|排行|推荐|"
    r"前\s*\d+(?!\d)(?!\s*(?:个\s*)?(?:交易日|日|天|周|星期|月|年|小时|分钟|根\s*[kK]?线))|"
    r"(?:上涨|下跌|涨幅|跌幅|市盈率|市值)[^。；;]{0,12}(?:的)?(?:股票|A股|ETF|基金)|"
    r"(?:股票|A股|ETF|基金)[^。；;]{0,16}(?:上涨|下跌|涨幅|跌幅|大于|小于|最高|最低))",
    re.IGNORECASE,
)
_DATA_QUERY_RE = re.compile(
    r"(?:查(?:询|一下|下)?|获取|告诉我|多少|是多少|当前|现在|最新|昨天|今日|"
    r"近\s*\d+\s*(?:年|月|日)|两年前|历史|走势|表现|数据)",
    re.IGNORECASE,
)
_DATA_TERM_RE = re.compile(
    r"(?:现价|股价|开盘价|收盘价|最高价|最低价|涨跌幅|涨幅|跌幅|振幅|"
    r"换手率|成交量|成交额|量比|市值|市盈率|市净率|市销率|股息率|"
    r"pe|pb|ps|pcf|"
    r"roe|营收|营业收入|净利润|归母净利润|毛利率|净利率|现金流|"
    r"主力流入|资金流向|盘口|估值|财务|行情)",
    re.IGNORECASE,
)
# Provider-owned aliases are intentionally broader than the executable strategy
# Catalog.  This router only decides whether to send an explicit lookup to the
# read-only finance-data service; it does not authorize a backtest condition.
# Keeping common financial-data noun shapes here lets searchData own the exact
# field/alias mapping instead of requiring every provider field to be copied
# into this application.
_PROVIDER_DATA_HINT_RE = re.compile(
    r"(?:股东|股本|持仓|机构|基金|北向|融资|融券|两融|余额|户数|家数|人数|"
    r"评级|目标价|分红|派息|股息|估值|利润|收入|营收|资产|负债|现金流|"
    r"市值|净值|价格|股价|涨跌|振幅|换手|成交|量比|份额|规模|"
    r"(?:比|率|额|量|数|值|价|倍数|天数))",
    re.IGNORECASE,
)
_TIME_OR_DATA_TERM_RE = re.compile(
    r"(?:当前|现在|最新|今天|今日|昨天|昨日|前天|两年前|近\s*\d+\s*(?:年|月|日)|"
    r"(?:19|20)\d{2}年|现价|股价|开盘价|收盘价|最高价|最低价|涨跌幅|"
    r"涨幅|跌幅|振幅|换手率|成交量|成交额|量比|市值|市盈率|市净率|"
    r"pe|pb|ps|pcf|"
    r"市销率|股息率|roe|营收|营业收入|净利润|归母净利润|毛利率|"
    r"净利率|现金流|主力流入|资金流向|盘口|估值|财务|行情)",
    re.IGNORECASE,
)
_TRAILING_QUERY_WORDS_RE = re.compile(
    r"(?:分别)?(?:是)?(?:多少|怎么样|如何|是什么|数据|情况)?[吗呢吧呀啊]?[?？。！!]*$",
    re.IGNORECASE,
)
_QUERY_LEAD_RE = re.compile(
    r"^(?:(?:请(?:问|帮我)?|麻烦(?:你)?|帮我|给我)\s*)?"
    r"(?:(?:查(?:询|一下|下)?|看(?:一下|下)?|获取|告诉我)\s*)?",
    re.IGNORECASE,
)
_DEICTIC_INSTRUMENT_RE = re.compile(r"^(?:它|这只|这家公司|该股|这个股票)(?:的)?")
_EXPLICIT_SECURITY_CODE_RE = re.compile(
    r"(?<!\d)\d{6}(?:\.(?:SH|SZ|BJ))?(?!\d)",
    re.IGNORECASE,
)
_TIME_TO_METRIC_CONNECTOR_RE = re.compile(
    r"^(?:(?:的|今天|今日|当天|当日|时候|期间)\s*)*$",
    re.IGNORECASE,
)
_SCREEN_FOLLOWUP_QUERY_RE = re.compile(
    r"(?:[；;，,]\s*(?:然后|并且|并|再|同时)?|(?:然后|并且|并|再|同时)\s*)"
    r"(?:再\s*)?(?:获取|查询|查(?:询|一下|下)?|看(?:一下|下)?|告诉我)\s*"
    r"(?P<request>.+)$",
    re.IGNORECASE,
)
_UNSUPPORTED_MARKET_RE = re.compile(
    r"(?:港股|港交所|香港股票|美股|纽交所|纳斯达克|NASDAQ|NYSE)",
    re.IGNORECASE,
)
_ETF_RE = re.compile(r"(?:ETF|交易型开放式指数基金)", re.IGNORECASE)
_ORDINARY_FUND_RE = re.compile(
    r"(?:基金|混合型|债券型|货币型|指数型基金)",
    re.IGNORECASE,
)
_STRATEGY_WORD_RE = re.compile(
    r"(?:买|卖|持有|回测|仓位|涨停|跌停|股价|价格|成交量|成交额|"
    r"macd|rsi|kdj|cci|boll|bbi|ema|obv|均线|金叉|死叉|放量|缩量|"
    r"市盈率|市净率|roe|营收|净利润|公告|年报|季报|短线|波段)",
    re.IGNORECASE,
)
_STRATEGY_ACTION_RE = re.compile(
    r"(?:买|卖|持有|回测|建仓|开仓|平仓|止盈|止损|仓位)",
    re.IGNORECASE,
)
_EXPLICIT_CONDITION_EDIT_RE = re.compile(
    r"(?:买入|买进|建仓|开仓|卖出|卖掉|平仓|退出)\s*"
    r"(?:(?:条件|规则|信号)\s*)?(?:改为|改成|修改为|调整为|换成|替换为)",
    re.IGNORECASE,
)
_CASUAL_RE = re.compile(
    r"(?:^(?:你好|您好|嗨|哈喽)|"
    r"^(?:我|你)(?:是|不是|喜欢|讨厌|觉得)|"
    r"吃什么|天气|星期几|今天几号|哈哈|呵呵|谢谢|谢了)",
    re.IGNORECASE,
)
_OBVIOUS_LIFESTYLE_RE = re.compile(
    r"^(?:(?:你好|您好|嗨|哈喽|hello)[，,\s]*)?"
    r"(?:(?:今天|今日)天气(?:怎么样|如何|好吗|好不好)?|"
    r"天气(?:怎么样|如何|好吗|好不好)|"
    r"今天会下雨吗|要带伞吗|"
    r"(?:今天|中午|晚上|宵夜)?吃什么|"
    r"现在几点(?:了)?|今天几号|今天星期几)"
    r"[吗呢吧呀啊？? 。！!]*$",
    re.IGNORECASE,
)
_CANCEL_RE = re.compile(r"^(?:算了|取消|不做了|不测了)$")
_ORDINAL_RE = re.compile(r"^(?:我?选|选择|用)?\s*(?:第)?\s*(?P<value>[123１２３一二三])\s*(?:个|项|条)?$")
_CHANGE_INSTRUMENT_PREFIX_RE = re.compile(
    r"^(?:标的改为|股票改为|改成|换成)\s*",
    re.IGNORECASE,
)
_CHANGE_INSTRUMENT_TARGET_RE = re.compile(
    r"(?:\d{6}(?:\.(?:SH|SZ|BJ))?|[一-鿿A-Za-z0-9*STst·\-]{2,32})",
    re.IGNORECASE,
)


def extract_change_instrument_target(utterance: str) -> str | None:
    """Extract an explicitly requested replacement instrument, without guessing."""

    normalized = utterance.strip()
    prefix = _CHANGE_INSTRUMENT_PREFIX_RE.match(normalized)
    if prefix is None:
        return None
    target = normalized[prefix.end() :].strip()
    for suffix in ("这个", "这只", "这支"):
        if target.endswith(suffix) and len(target) > len(suffix):
            target = target[: -len(suffix)].strip()
            break
    if _CHANGE_INSTRUMENT_TARGET_RE.fullmatch(target) is None:
        return None
    return target


def classify_clarification_turn(
    utterance: str,
    *,
    has_options: bool = False,
) -> TurnIntent:
    """Classify the turn before the old clarification sentence is merged."""

    if _SAFETY_RE.search(utterance):
        return TurnIntent.SAFETY

    normalized = utterance.strip()
    if not normalized:
        return TurnIntent.UNKNOWN
    if _ORDINAL_RE.fullmatch(normalized) is not None:
        return TurnIntent.SELECT_OPTION if has_options else TurnIntent.UNKNOWN
    if _CANCEL_RE.fullmatch(normalized):
        return TurnIntent.CANCEL
    if extract_change_instrument_target(normalized) is not None:
        return TurnIntent.CHANGE_INSTRUMENT
    semantic_text = _CONTINUATION_PREFIX_RE.sub("", normalized, count=1).strip()
    if not semantic_text:
        semantic_text = normalized
    # A condition-edit instruction is model input, not a request to look up
    # the numbers or historical window contained in the replacement rule.
    # This only selects the editor; the model still interprets the change.
    if _EXPLICIT_CONDITION_EDIT_RE.search(semantic_text):
        return TurnIntent.SUPPLEMENT
    # Familiar but underspecified trading intuitions deserve bounded strategy
    # choices, not a casual-chat response.
    if _VAGUE_STRATEGY_RE.search(semantic_text):
        return TurnIntent.VAGUE_STRATEGY
    # A negated or explicitly absent exit is a missing slot, not a complete
    # buy/sell rule.  Mask only that phrase before checking action presence;
    # the original sentence still proceeds to the compiler unchanged.
    action_text = _NEGATED_SELL_RE.sub("", semantic_text)
    if _BUY_RE.search(action_text) and _SELL_RE.search(action_text):
        return TurnIntent.NEW_STRATEGY
    # Explicit screening/current-data language takes precedence over the
    # generic ``怎么样 / 咋样`` viewpoint wording. Otherwise a question
    # such as ``东方财富换手率怎么样`` is incorrectly routed to idea
    # generation even though both the entity and requested metric are clear.
    if _SCREEN_QUERY_RE.search(normalized) or (
        _DATA_QUERY_RE.search(normalized)
        and (_DATA_TERM_RE.search(normalized) or _PROVIDER_DATA_HINT_RE.search(normalized))
    ):
        return TurnIntent.DATA_QUERY
    # Short known-entity lookups are often phrased without a question verb,
    # for example ``东方财富换手率`` or ``同花顺PE``.  Treat them as data
    # queries only when no trading action is present, so a rule such as
    # ``市盈率低于20买入`` still reaches the strategy compiler.
    if _DATA_TERM_RE.search(normalized) and _STRATEGY_ACTION_RE.search(normalized) is None:
        return TurnIntent.DATA_QUERY
    # Keep unmistakable day-to-day small talk away from the web-research and
    # idea routes.  This check is intentionally narrow: cross-domain opinions
    # such as "AI 会不会是泡沫" or "我讨厌特朗普" still reach the
    # bounded research path, while a literal weather or meal question does not.
    if (
        _OBVIOUS_LIFESTYLE_RE.fullmatch(normalized) is not None
        and _STRATEGY_WORD_RE.search(normalized) is None
    ):
        return TurnIntent.CASUAL
    # A bare Latin token can be a person, product, slang word or typo.  Until
    # the user or research grounds what it means, routing a sentence such as
    # ``我讨厌wash`` into investment-idea generation invents a market premise
    # and can also contaminate a pending viewpoint.  Known trading syntax was
    # already handled above, so fail closed as conversation here.
    if _AMBIGUOUS_LATIN_PREFERENCE_RE.fullmatch(normalized) is not None:
        return TurnIntent.CASUAL
    if _VIEWPOINT_RE.search(semantic_text):
        return TurnIntent.VIEWPOINT
    if _CASUAL_RE.search(normalized) and _STRATEGY_WORD_RE.search(normalized) is None:
        return TurnIntent.CASUAL
    if _STRATEGY_WORD_RE.search(normalized):
        return TurnIntent.SUPPLEMENT
    return TurnIntent.UNKNOWN


def has_explicitly_missing_exit(utterance: str) -> bool:
    """Return whether the user explicitly says that no exit rule exists."""

    return _NEGATED_SELL_RE.search(utterance.strip()) is not None


def continues_prior_viewpoint(utterance: str) -> bool:
    """Return whether a sentence explicitly adds to a preceding viewpoint.

    This is intentionally narrower than generic viewpoint detection.  It
    requires an overt discourse connector and an opinion-bearing continuation,
    so a fresh strategy or an arbitrary unknown sentence is never merged into
    the previous idea merely because a clarification is open.
    """

    normalized = utterance.strip()
    if _AMBIGUOUS_LATIN_PREFERENCE_RE.fullmatch(normalized) is not None:
        return False
    prefix = _CONTINUATION_PREFIX_RE.match(normalized)
    if prefix is None:
        return False
    continuation = normalized[prefix.end() :].strip()
    return bool(
        continuation
        and _VIEWPOINT_RE.search(continuation)
        and _STRATEGY_ACTION_RE.search(continuation) is None
    )


def data_query_uses_screening(utterance: str) -> bool:
    """Route discovery/ranking queries to the screener, not single-name lookup."""

    return _SCREEN_QUERY_RE.search(utterance.strip()) is not None


def data_query_needs_instrument_context(utterance: str) -> bool:
    """Return whether a lookup may reuse an already verified instrument.

    This is deliberately narrower than entity recognition.  It only accepts a
    deictic or entity-omitted metric lookup such as ``它的最新市盈率`` or
    ``昨天换手率多少``.  A company name or security code is never replaced by
    old clarification context.  Callers must keep ``utterance`` as the audited
    user text and may use the verified context only to build the provider
    query.
    """

    normalized = utterance.strip()
    if (
        classify_clarification_turn(normalized) is not TurnIntent.DATA_QUERY
        or data_query_uses_screening(normalized)
        or _EXPLICIT_SECURITY_CODE_RE.search(normalized) is not None
    ):
        return False

    query_body = _QUERY_LEAD_RE.sub("", normalized, count=1).lstrip(" ，,：:")
    if _DEICTIC_INSTRUMENT_RE.match(query_body) is not None:
        return True

    first_term = _TIME_OR_DATA_TERM_RE.search(query_body)
    if first_term is None or first_term.start() != 0:
        provider_term = _PROVIDER_DATA_HINT_RE.search(query_body)
        return provider_term is not None and provider_term.start() == 0
    if _DATA_TERM_RE.fullmatch(first_term.group(0)) is not None:
        return True

    remainder = query_body[first_term.end() :]
    metric = _DATA_TERM_RE.search(remainder) or _PROVIDER_DATA_HINT_RE.search(remainder)
    if metric is None:
        return False
    return _TIME_TO_METRIC_CONNECTOR_RE.fullmatch(remainder[: metric.start()]) is not None


def data_query_asset_type(utterance: str) -> str:
    """Keep the provider's supported product names without inventing a mapping."""

    normalized = utterance.casefold()
    if "etf" in normalized:
        return "ETF"
    if "基金" in utterance:
        return "基金"
    return "A股"


def data_query_unsupported_scope(utterance: str) -> str | None:
    """Reject an explicitly requested product outside the current scope.

    Live lookups currently support A-share stocks and exchange-traded ETFs.
    Explicit Hong Kong, US or ordinary-fund requests must not silently fall
    through to the provider's ``A股`` default.
    """

    normalized = utterance.strip()
    match = _UNSUPPORTED_MARKET_RE.search(normalized)
    if match is not None:
        value = match.group(0).casefold()
        return "美股" if value in {"美股", "纽交所", "纳斯达克", "nasdaq", "nyse"} else "港股"
    if _ETF_RE.search(normalized) is None and _ORDINARY_FUND_RE.search(normalized) is not None:
        return "普通基金"
    return None


def extract_data_query_indicators(utterance: str) -> str | None:
    """Keep the user's own metric/time phrase while excluding the entity prefix.

    The finance skill still receives the complete query for its own entity
    recognition.  This value is only its required indicator hint; no synonym,
    timeframe or metric is added by the service.
    """

    normalized = utterance.strip()
    marker = _TIME_OR_DATA_TERM_RE.search(normalized) or _PROVIDER_DATA_HINT_RE.search(normalized)
    if marker is None:
        return None
    indicator_text = normalized[marker.start() :]
    indicator_text = _TRAILING_QUERY_WORDS_RE.sub("", indicator_text).strip(" ，,。；;?？")
    return indicator_text or None


def extract_screened_finance_indicators(utterance: str) -> str | None:
    """Return only an explicitly requested post-screening data lookup.

    A phrase such as ``涨幅前5只股票`` describes the screening condition; it
    is not a request to call the finance-data API again.  The composed path is
    therefore enabled only by a clearly separated second request such as
    "then fetch ten years of attributable net profit".
    """

    match = _SCREEN_FOLLOWUP_QUERY_RE.search(utterance.strip())
    if match is None:
        return None
    request = match.group("request").strip()
    return extract_data_query_indicators(request) or request.strip(" ，,。；;?？") or None


def extract_screening_query(utterance: str) -> str:
    """Keep only the discovery clause of a composed screen-and-query request.

    ``selectSecurity`` receives the user's screening condition, while the
    explicitly separated follow-up metric is sent to ``searchData``.  Sending
    both clauses to the screener can change its result columns and hide the
    security identity needed by the second call.
    """

    normalized = utterance.strip()
    match = _SCREEN_FOLLOWUP_QUERY_RE.search(normalized)
    if match is None:
        return normalized
    screening_query = normalized[: match.start()].strip(" ，,。；;?？")
    return screening_query or normalized
