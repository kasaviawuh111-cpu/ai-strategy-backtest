"""Use case for turning one sentence into a validated Strategy DSL revision."""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum
from itertools import pairwise
from typing import Literal
from zoneinfo import ZoneInfo

from ashare_lab.application.backtest_submission import resolve_execution_settings
from ashare_lab.application.clarification_guidance import (
    build_clarification_guidance,
    merge_clarification_supplement,
)
from ashare_lab.application.turn_intent import (
    TurnIntent,
    classify_clarification_turn,
    continues_prior_viewpoint,
    has_explicitly_missing_exit,
)
from ashare_lab.domain.catalog import CatalogSnapshot
from ashare_lab.domain.market_data import AshareInstrumentCodeError, normalize_a_share_instrument
from ashare_lab.domain.strategy import (
    AllCondition,
    AnyCondition,
    BacktestConfig,
    CatalogRef,
    DailyExecutionPolicy,
    EventCondition,
    EventDocumentTextPredicate,
    FinancialConditionV1,
    FirstOfExit,
    HoldingPeriodExit,
    IndicatorCondition,
    Instrument,
    PositionReturnExit,
    StrategyCatalogError,
    StrategySpec,
    TrailingDrawdownExit,
    canonical_hash,
    iter_holding_period_exits,
    iter_indicator_conditions,
    iter_position_return_exits,
    iter_trailing_drawdown_exits,
    strategy_requires_events,
    strategy_requires_financials,
    validate_strategy_against_catalog,
)
from ashare_lab.domain.strategy.models import Condition, ExitRule
from ashare_lab.ports.candidate_generation import (
    CandidateAst,
    CandidateGenerator,
    CandidateGroundingEvidence,
    CandidateProvenance,
    CompileInput,
    EventIntent,
    ExitIntent,
    FinancialIntent,
    HoldingPeriodIntent,
    IndicatorIntent,
    PositionReturnIntent,
    SignalIntent,
    TrailingDrawdownIntent,
)
from ashare_lab.ports.clarification_dialogue import (
    ClarificationDialogueAssessment,
    ClarificationDialogueRequest,
    ClarificationDialogueRouter,
    ClarificationDialogueTurn,
    ClarificationOption,
)
from ashare_lab.ports.dialogue_progress import emit_progress
from ashare_lab.ports.execution_settings import ExecutionSettingsPatch
from ashare_lab.ports.idea_routing import (
    IdeaGenerationError,
    IdeaProposal,
    IdeaResearchUnavailableError,
    IdeaRoute,
    IdeaRouter,
    UnboundIdeaStrategy,
)
from ashare_lab.ports.instrument_resolution import InstrumentNameAmbiguous, InstrumentNameCandidate
from ashare_lab.ports.strategy_advice import StockRecommendation
from ashare_lab.ports.strategy_editing import StrategyEditor, StrategyEditRequest

DEFAULT_INITIAL_CASH_CNY = 1_000_000
SHANGHAI = ZoneInfo("Asia/Shanghai")
POSITION_AWARE_EXIT_AND_UNSUPPORTED = "position_aware_exit_and_not_supported"
_LOGGER = logging.getLogger(__name__)
_IDEA_ROUTE_DIAGNOSTIC_CODES = frozenset({"no_supported_signal_recognized"})
_INSTRUMENT_CLARIFICATION_CODES = frozenset(
    {
        "instrument_required",
        "instrument_unconfirmed",
        "instrument_resolution_unavailable",
    }
)
_LOCAL_CLARIFICATION_ROUTE_CODES = frozenset(
    {
        "entry_rule_not_recognized",
        "exit_rule_not_recognized",
        "strategy_rule_incomplete",
        "ambiguous_obv_direction",
        "ambiguous_volume_direction",
        "ambiguous_boolean_expression",
        "ambiguous_cross_indicator",
    }
)
_STRATEGY_SYNTAX_MARKERS = (
    "买入",
    "买进",
    "建仓",
    "开仓",
    "卖出",
    "卖掉",
    "退出",
    "平仓",
    "止盈",
    "止损",
    "持有",
    "回测",
    "macd",
    "rsi",
    "kdj",
    "cci",
    "boll",
    "bbi",
    "ema",
    "均线",
    "股价",
    "价格",
    "成交量",
    "成交额",
    "营收",
    "营业收入",
    "净利润",
    "毛利率",
    "净利率",
    "roe",
    "rota",
    "市盈率",
    "市净率",
    "市销率",
    "市现率",
    "金叉",
    "死叉",
    "年报",
    "半年报",
    "季报",
    "业绩预告",
    "公告",
    "中标",
    "许可",
)
_CLARIFICATION_NEGATION_RE = re.compile(
    r"(?:不想(?:用|要|选)?|不打算|不考虑|不接受|不要|不用|别用|拒绝|排除)"
)
_CLARIFICATION_QUESTION_RE = re.compile(
    r"(?:你觉得|你认为|是不是|能不能|可不可以|合适吗|好吗|行吗|怎么样|如何|[吗呢][？?]?$|[？?]$)"
)
_CLARIFICATION_EXAMPLE_RE = re.compile(r"^(?:比如|例如|举例|比方说|譬如|打个比方)")
_CLARIFICATION_CONVERSATION_RE = re.compile(
    r"^(?:[!！?？。…]+|[123一二三]|"
    r"我(?:是|不是|喜欢|讨厌|觉得).+|"
    r"你(?:是|不是).+|"
    r"哈哈+|呵呵+|谢谢|谢了|好的|好吧|算了|取消)$"
)
_INITIAL_CONVERSATION_RE = re.compile(
    r"^(?:[!！?？。…]+|我(?:是|不是).+|你(?:是|不是).+|"
    r"哈哈+|呵呵+|谢谢|谢了|好的|好吧|算了|取消)$",
    re.IGNORECASE,
)
_LEADING_GREETING_RE = re.compile(
    r"^(?:你好|您好|嗨|哈喽|hello)[,，。!！?？\s]*",
    re.IGNORECASE,
)
_VIEWPOINT_INSTRUMENT_RE = re.compile(
    r"(?:我)?(?:看好|看多|关注|喜欢)\s*"
    r"(?P<name>[\u4e00-\u9fffA-Za-z0-9*STst·\-]{2,32})(?:[,，。！!？?]|$)"
)
_STOCK_FIRST_IDEA_GUIDANCE_CUE_RE = re.compile(
    r"(?:怎么样|咋样|如何|怎么看|行不行|可以吗|可不可以|能不能|"
    r"值不值得|给我(?:来)?(?:两|二|三|\d+)?个?策略|"
    r"给我[^。；;!！?？]{0,18}策略|"
    r"做(?:个|几个|一套)?策略|"
    r"什么策略|怎么做|低买高卖|高抛低吸|低吸高抛|"
    r"抄底|做波段|做短线|短线怎么做)",
    re.IGNORECASE,
)
_INFERABLE_TRADING_INTUITION_RE = re.compile(
    r"(?:低买高卖|高抛低吸|低吸高抛|超跌反弹)",
    re.IGNORECASE,
)
_NEGATED_VIEWPOINT_INSTRUMENT_RE = re.compile(r"(?:不|不太|不怎么|并不)(?:看好|看多|关注|喜欢)")
_IDEA_GUIDANCE_SECURITY_CODE_RE = re.compile(
    r"(?<!\d)\d{6}(?:\.(?:SH|SZ|BJ))?(?!\d)",
    re.IGNORECASE,
)
_ANALYZE_INSTRUMENT_RE = re.compile(
    r"(?:分析|研究|看看)(?:一下)?\s*"
    r"(?P<name>[\u4e00-\u9fffA-Za-z0-9*STst·\-]{2,32}?)"
    r"(?=(?:的|[，,。！!？?；;]|并|然后|给我|$))",
    re.IGNORECASE,
)
_POSITION_AWARE_EXIT_AND_EXPLANATION = (
    "当前回测只支持把持有期、止盈、止损、回撤与其他卖出条件按“任一先触发即卖出”执行，"
    "尚不能正确执行“同时满足才卖出”。请改用“或”，或只保留一个这类卖出条件。"
)

_NON_DAILY_TIMEFRAME_RE = re.compile(
    r"(?:\d{1,4}\s*(?:分钟(?:线)?|小时(?:线)?|分(?:k|线))|"
    r"\d{1,4}\s*[-_]?\s*(?:mins?|minutes?|m|hours?|hrs?|h)(?![a-z])|"
    r"分时|分钟级|小时级|周线|月线|周频|月频|盘中|日内|实时信号)",
    re.IGNORECASE,
)
_SAME_SESSION_EXECUTION_RE = re.compile(
    r"(?:(?:当日|当天|今日|同日|本交易日)[^,，。；;]{0,12}"
    r"(?:买入|卖出|买进|卖掉|下单|成交(?!量|额)|买|卖)|"
    r"(?:马上|立刻|立即|即时)[^,，。；;]{0,6}"
    r"(?:买入|卖出|买进|卖掉|下单|成交(?!量|额)|买|卖)|"
    r"(?:买入|卖出|买进|卖掉|下单|成交(?!量|额)|买|卖)"
    r"[^,，。；;]{0,4}(?:马上|立刻|立即|即时))"
)
_DAILY_VOLUME_OBSERVATION_PREFIX_RE = re.compile(
    r"(?:当日|当天|今日|同日|本交易日)(?=\s*(?:的\s*)?成交(?:量|额))"
)
_DAILY_RETURN_ACTION_RE = re.compile(
    r"(?:当日|当天|今日)(?:上涨|下跌|涨幅|(?<!涨)跌幅|涨|(?<!涨)跌)了?"
    r"(?:不低于|不小于|不少于|大于等于|不高于|不大于|"
    r"不超过|小于等于|超过|高于|大于|低于|小于|超|达到|到|至少|至多|"
    r">=|<=|≥|≤|>|<)?"
    r"\d+(?:\.\d+)?\s*[%％](?:以上|及以上|以下|及以下)?"
    r"(?:时|则|就)?\s*(?:买入|卖出|买进|卖掉|买|卖)"
)
_NON_DEFAULT_EXECUTION_RE = re.compile(
    r"(?:下一|下个|次)(?:个)?(?:可交易)?(?:交易日|日)"
    r"[^,，。；;]{0,12}"
    r"(?:收盘(?:价)?|尾盘|盘中|开盘后|\d{1,2}[:：]\d{2})"
    r"[^,，。；;]{0,12}(?:买入|卖出|下单|成交)"
)
_DELAY_COUNT = r"(?:\d{1,4}|[零〇一二两三四五六七八九十百千]+)"
_DELAY_UNIT = r"(?:交易日|交易天|天|日)"
_DELAY_EXPRESSION = (
    rf"(?:第{_DELAY_COUNT}(?:个)?{_DELAY_UNIT}|"
    rf"{_DELAY_COUNT}(?:个)?{_DELAY_UNIT}(?:之后|以后|后)|"
    rf"(?:之后|以后|后)\s*(?:第)?{_DELAY_COUNT}(?:个)?{_DELAY_UNIT}|"
    r"隔天|隔日)"
)
_EXPLICIT_DELAYED_EXECUTION_RE = re.compile(
    rf"{_DELAY_EXPRESSION}[^,，。；;]{{0,12}}"
    r"(?P<action>买入|买进|建仓|下单|成交|卖出|卖掉|平仓|清仓)"
)
_SUPPORTED_HOLDING_EXIT_RE = re.compile(
    r"(?:(?:买入)?成交后|买入后|持有)(?:第)?\d{1,4}(?:个)?"
    r"(?:交易日|交易天|天|日)(?:后|时|到期)?"
    r"[^,，。；;]{0,8}(?:卖出|卖掉|平仓|清仓)"
)
_SUPPORTED_BARE_HOLDING_EXIT_RE = re.compile(
    r"\s*(?:后)?(?:第)?\d{1,4}(?:个)?(?:交易日|交易天|天|日)"
    r"(?:后|时|到期)?\s*(?:卖出|卖掉|平仓|清仓)\s*"
)
_PREVIOUS_SESSION_LIMIT_UP_ENTRY_RE = re.compile(
    r"涨停[^。；;!！?？]{0,20}"
    r"(?:次日|翌日|第二天|第2天|下一个交易日|下一交易日|下个交易日|隔日|隔天)"
    r"[^,，。；;]{0,8}(?:买入|买进|建仓|开仓|(?<!购)买)"
)
_SPECIFIC_REPORT_PERIOD_RE = re.compile(
    r"(?:(?:19|20)\d{2}(?:年(?:的)?)?(?:年度报告|年报|半年度报告|半年报|中报|"
    r"季度报告|季报|第?[一二三四1234]季度报告|[一二三四1234]季报)|"
    r"(?<!\d)\d{2}(?:年(?:的)?)?(?:年度报告|年报|半年度报告|半年报|中报|"
    r"季度报告|季报|第?[一二三四1234]季度报告|[一二三四1234]季报)|"
    r"(?:今年|去年|前年|上年|本年|当年)(?:的|发布的|公布的)?"
    r"(?:年度报告|年报|半年度报告|半年报|中报|季度报告|季报)|"
    r"(?:第?[一二三四1234]季度报告|[一二三四1234]季报))"
)
_PERIODIC_REPORT_RE = re.compile(
    r"(?:业绩预告|业绩快报|定期报告|年度报告|年报|半年度报告|半年报|中报|季度报告|季报)"
)
_UNMODELED_REPORT_FILTER_RE = re.compile(
    r"(?:预增|预减|预亏|扭亏|续盈|首亏|略增|略减|亏损|"
    r"大幅(?:增长|上升|下降|下滑)|利润|净利|营业收入|营收|扣非|"
    r"同比|环比|增长\s*\d+(?:\.\d+)?%|下降\s*\d+(?:\.\d+)?%)"
)
_MACD_UNMODELED_QUALIFIER_RE = re.compile(
    r"(?:macd[^,，。；;]{0,16}(?:零轴上方|零轴之上|低位)|"
    r"(?:低位|零轴上方|零轴之上)[^,，。；;]{0,16}macd)",
    re.IGNORECASE,
)
_SOURCE_ACTION_RE = re.compile(r"(?<!超)(?:买入|卖出|买进|卖掉|(?<!购)买|卖)")
_NAMED_INDICATOR_RE = re.compile(
    r"(?:"
    r"macd|rsi|kdj|cci|bbi|obv|ema|"
    r"(?<![a-z])(?:ma|atr|natr|adx|dmi|roc|mom|momentum|wr|"
    r"stoch(?:astic)?|boll(?:inger)?|donchian|bias)(?![a-z])|"
    r"均线|移动平均|指数移动平均|指数均线|布林|乖离率|"
    r"能量潮|成交量|相对成交量|量比|成交额|"
    r"平均真实波幅|真实波幅|历史波动率|年化波动率|"
    r"趋向指标|平均趋向指数|随机指标|随机振荡指标|"
    r"威廉指标|唐奇安|振幅|涨跌幅|收益率|连涨|新高|阶段趋势"
    r")",
    re.IGNORECASE,
)
_EXPLICIT_INDICATOR_TRIGGER_RE = re.compile(
    r"(?:[a-z]+(?:_[a-z]+)+|"
    r"(?:above|below|rising|falling|uptrend|downtrend|range|breakout|breakdown|"
    r"bullish|bearish)(?![a-z])|"
    r"金叉|死叉|上穿|下穿|突破|跌破|站上|失守|"
    r"高于|低于|大于|小于|超过|不少于|不低于|不高于|不大于|不超过|"
    r"至少|至多|等于|达到|介于|之间|[<>≥≤]|"
    r"超买|超卖|低位|高位|上方|下方|多头|空头|"
    r"上升|下降|上涨|下跌|向上|向下|大涨|大跌|走强|走弱|转强|转弱|"
    r"放量|缩量|背离|齐升|齐跌|新高|连涨|趋势|"
    r"红柱|绿柱|零轴|0轴)",
    re.IGNORECASE,
)

_SOURCE_SEMANTIC_EXPLANATIONS = {
    "previous_session_limit_up_capability_unavailable": (
        "已理解为‘前一交易日涨停、下一交易日买入’，但这个信号还缺"
        "逐证券逐交易日的涨停价/涨停状态，以及 DSL 的前一交易日引用；"
        "不能用单日涨 10% 替代。‘做个短线’也没有说清卖出方式，请补充持有天数、"
        "止盈止损或技术卖出条件。原话会保留，系统不会猜。"
    ),
    "non_daily_timeframe_not_supported": (
        "当前正式回测只支持日线收盘确认，不能把分钟、盘中、周线或月线信号改成日线执行。"
    ),
    "same_session_execution_not_supported": (
        "当前正式回测只支持信号确认后在下一可交易日开盘尝试成交，不支持当天或立即成交。"
    ),
    "execution_price_time_not_supported": (
        "当前正式日线回测只能在下一可交易日开盘尝试成交，不能把你指定的"
        "下一交易日收盘、尾盘或具体时点改成开盘执行。"
    ),
    "event_report_period_filter_not_supported": (
        "当前事件条件还不能精确限定某一报告年份或第几季度，因此不会扩大成匹配所有报告。"
    ),
    "event_attribute_filter_not_supported": (
        "当前可以按报告发布回测，但还不能按业绩方向、净利润或增长幅度过滤，因此不会丢掉限定条件后假装成功。"
    ),
    "technical_qualifier_not_supported": (
        "当前 MACD 支持目录中已发布的独立触发条件，还不能将‘低位’或"
        "‘零轴上方金叉’这类组合限定精确执行。"
    ),
    "indicator_trigger_requires_clarification": (
        "我识别到了指标和买卖动作，但没有识别到明确触发条件。请一次写清每个条件，"
        "例如‘MACD 金叉买入、死叉卖出’或‘RSI 低于 30 买入、高于 70 卖出’；"
        "系统不会替你补默认触发规则。"
    ),
}


class _UnsupportedCandidateSemantics(ValueError):
    def __init__(self, diagnostic_code: str) -> None:
        super().__init__(diagnostic_code)
        self.diagnostic_code = diagnostic_code


class CompileStatus(StrEnum):
    READY = "ready"
    NEEDS_CLARIFICATION = "needs_clarification"
    UNSUPPORTED = "unsupported"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class FieldProvenance:
    path: str
    source: str


@dataclass(frozen=True, slots=True)
class CandidateRejectionSummary:
    candidate_rank: int
    diagnostic_code: str


@dataclass(frozen=True, slots=True)
class CandidateAlternativeSummary:
    candidate_rank: int
    strategy_hash: str


@dataclass(frozen=True, slots=True)
class _InstrumentMention:
    name: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _ValidatedIdeaProposal:
    proposal: IdeaProposal
    strategy: StrategySpec
    strategy_hash: str


@dataclass(frozen=True, slots=True)
class CompileOutcome:
    status: CompileStatus
    strategy: StrategySpec | None = None
    strategy_hash: str | None = None
    clarification: str | None = None
    diagnostic_code: str | None = None
    provenance: tuple[FieldProvenance, ...] = ()
    candidate_provenance: CandidateProvenance | None = None
    candidate_grounding: tuple[CandidateGroundingEvidence, ...] = ()
    candidate_rejections: tuple[CandidateRejectionSummary, ...] = ()
    candidate_alternatives: tuple[CandidateAlternativeSummary, ...] = ()
    idea_route: IdeaRoute | None = None
    # A provisional strategy is deliberately distinct from ``strategy``.  It
    # is a server-compiled reading of a familiar trading expression (for
    # example, "低买高卖"), shown to the user for confirmation only.  It never
    # makes this outcome executable by itself.
    suggested_strategy: StrategySpec | None = None
    suggested_strategy_hash: str | None = None
    suggested_strategy_choice_id: str | None = None
    suggested_strategy_note: str | None = None
    # Internal only: keep the chosen model rules intact while asking for a stock.
    selected_idea_proposal: IdeaProposal | None = None
    # Keep the accepted rules while clarifying an ambiguous multi-turn edit.
    revision_base_strategy: StrategySpec | None = None
    instrument_suggestion_declined: bool = False
    stock_recommendations: tuple[StockRecommendation, ...] = ()
    run_requested: bool = False
    refresh_data: bool = False
    is_strategy_edit: bool = False
    instrument_candidates: tuple[InstrumentNameCandidate, ...] = ()
    # Internal pending intent is not execution authorization on a clarification.
    pending_edit_run_requested: bool = False
    pending_edit_refresh_data: bool = False
    execution_settings: ExecutionSettingsPatch = field(default_factory=ExecutionSettingsPatch)
    pending_execution_settings: ExecutionSettingsPatch = field(
        default_factory=ExecutionSettingsPatch,
    )


@dataclass(frozen=True, slots=True)
class ClarificationSuggestion:
    id: str
    title: str
    preview: str


@dataclass(frozen=True, slots=True)
class ClarificationTurnOutcome:
    reply_kind: Literal["accepted", "clarification"]
    assistant_message: str
    outcome: CompileOutcome
    compile_input: CompileInput
    revision_changed: bool
    suggestions: tuple[ClarificationSuggestion, ...] = ()


class StrategyCompiler:
    def __init__(
        self,
        *,
        generator: CandidateGenerator,
        catalog: CatalogSnapshot,
        catalog_id: str,
        release_version: str,
        lookback_years: int = 1,
        initial_cash_cny: int = DEFAULT_INITIAL_CASH_CNY,
        trusted_date_provider: Callable[[], date] | None = None,
        backtest_anchor_date: date | None = None,
        idea_router: IdeaRouter | None = None,
        clarification_dialogue_router: ClarificationDialogueRouter | None = None,
        instrument_name_resolver: Callable[[str], str] | None = None,
        strategy_editor: StrategyEditor | None = None,
    ) -> None:
        self._generator = generator
        self._catalog = catalog
        self._catalog_id = catalog_id
        self._release_version = release_version
        self._lookback_years = lookback_years
        self._initial_cash_cny = initial_cash_cny
        self._trusted_date_provider = trusted_date_provider or _shanghai_today
        self._backtest_anchor_date = backtest_anchor_date
        self._idea_router = idea_router
        self._clarification_dialogue_router = clarification_dialogue_router
        self._instrument_name_resolver = instrument_name_resolver
        self._strategy_editor = strategy_editor

    async def edit_current_strategy(
        self, *, original_input: CompileInput, prior_outcome: CompileOutcome,
        answer: str, recent_turns: tuple[ClarificationDialogueTurn, ...] = (),
        backtest_results: tuple[Mapping[str, object], ...] = (),
    ) -> ClarificationTurnOutcome | None:
        base = prior_outcome.strategy or prior_outcome.revision_base_strategy
        if base is None or self._strategy_editor is None:
            return None
        as_of = self._backtest_anchor_date or self._trusted_date_provider()
        current_settings = resolve_execution_settings(prior_outcome.execution_settings)
        result = await self._strategy_editor.edit(StrategyEditRequest(
            answer=answer, prior_utterance=original_input.utterance, strategy=base,
            as_of_date=as_of, recent_turns=recent_turns[-20:], backtest_results=backtest_results,
            pending_clarification=prior_outcome.clarification,
            pending_run_requested=prior_outcome.pending_edit_run_requested,
            pending_refresh_data=prior_outcome.pending_edit_refresh_data,
            instrument_candidates=prior_outcome.instrument_candidates,
            execution_settings=current_settings,
            pending_execution_settings=prior_outcome.pending_execution_settings,
        ))
        if result is not None and result.disposition == "not_edit":
            return None
        if result is not None and result.disposition == "change_instrument":
            # The model names the target; the existing resolver and binder own
            # the security identity and retain the exact server-side rules.
            refs = tuple(_original_instrument_reference(ref, answer) or ""
                         for ref in result.instrument_refs)
            symbols: list[str | None] = []
            candidates: dict[str, InstrumentNameCandidate] = {}
            resolution_unavailable = False
            if 1 <= len(refs) <= 2 and all(ref and ref in answer for ref in refs):
                for ref in refs:
                    confirmed = [
                        item for item in prior_outcome.instrument_candidates
                        if ref.casefold() in {item.name.casefold(), item.symbol.casefold()}
                    ]
                    if len(confirmed) == 1:
                        symbols.append(confirmed[0].symbol)
                        continue
                    try:
                        symbols.append(await self.resolve_instrument_context(
                            ref, require_details=True,
                        ))
                    except InstrumentNameAmbiguous as exc:
                        symbols.append(None)
                        candidates.update((item.symbol, item) for item in exc.candidates)
                    except (OSError, TimeoutError):
                        symbols.append(None)
                        resolution_unavailable = True
                    except LookupError:
                        symbols.append(None)
            symbol = (symbols[0]
                      if symbols and None not in symbols and len(set(symbols)) == 1 else None)
            rebound_input = CompileInput(
                utterance=answer.strip(), instrument_context=symbol, as_of_date=as_of,
            )
            rebound = self.rebind_current_strategy(rebound_input, prior_outcome) if symbol else None
            if rebound is not None:
                return ClarificationTurnOutcome(
                    reply_kind="accepted", assistant_message=result.message,
                    outcome=replace(
                        rebound, candidate_provenance=result.provenance,
                        is_strategy_edit=True,
                        execution_settings=current_settings.merged(
                            prior_outcome.pending_execution_settings,
                        ).merged(result.execution_settings),
                        run_requested=bool(backtest_results and result.run_requested),
                        refresh_data=bool(
                            backtest_results and result.run_requested and result.refresh_data
                        ),
                        candidate_grounding=(CandidateGroundingEvidence(
                            path="/instrument/symbol", start=answer.index(refs[0]),
                            end=answer.index(refs[0]) + len(refs[0]), text=refs[0],
                        ),),
                    ),
                    compile_input=rebound_input, revision_changed=True,
                )
            message = "这次股票名称或代码还没核对上，请确认要用哪一只；原买卖规则已保留。"
            if resolution_unavailable:
                message = "东方财富选股 Skill 暂时无法核对股票名称，请稍后重试；原策略已保留。"
                candidates.clear()
            elif candidates:
                identities = "、".join(item.name for item in candidates.values())
                message = f"你说的“{refs[0]}”，具体是{identities}中的哪一只？"
            _LOGGER.warning(
                "instrument_change_unresolved ref_count=%s exact_spans=%s resolved=%s",
                len(refs), tuple(bool(ref) and ref in answer for ref in refs), tuple(symbols),
            )
            if symbols and None not in symbols and len(set(symbols)) > 1:
                known_names = {
                    item.verified_instrument["symbol"]: item.verified_instrument["name"]
                    for item in recent_turns
                    if item.verified_instrument is not None
                    and item.verified_instrument.get("symbol")
                    and item.verified_instrument.get("name")
                }
                identities = "、".join(
                    f"{known_names.get(resolved) or ref}（{resolved}）"
                    for ref, resolved in zip(refs, symbols, strict=True)
                )
                message = f"名称和代码对应不同股票：{identities}。你要用哪一只？原买卖规则已保留。"
            return ClarificationTurnOutcome(
                reply_kind="clarification", assistant_message=message,
                outcome=CompileOutcome(
                    status=CompileStatus.NEEDS_CLARIFICATION, clarification=message,
                    diagnostic_code="strategy_edit_clarification", revision_base_strategy=base,
                    candidate_provenance=result.provenance,
                    is_strategy_edit=True,
                    instrument_candidates=tuple(candidates.values())[:3],
                    execution_settings=current_settings,
                    pending_execution_settings=prior_outcome.pending_execution_settings.merged(
                        result.execution_settings,
                    ),
                    pending_edit_run_requested=result.run_requested,
                    pending_edit_refresh_data=result.refresh_data,
                ),
                compile_input=original_input, revision_changed=True,
            )
        if result is not None and result.disposition in {"discuss", "request_optimization"}:
            return ClarificationTurnOutcome(
                reply_kind="clarification", assistant_message=result.message,
                outcome=CompileOutcome(
                    status=CompileStatus.NEEDS_CLARIFICATION, clarification=result.message,
                    diagnostic_code=("strategy_optimization_requested"
                                     if result.disposition == "request_optimization"
                                     else "strategy_discussion"),
                    revision_base_strategy=base,
                    candidate_provenance=result.provenance,
                    execution_settings=current_settings,
                    is_strategy_edit=True,
                ),
                compile_input=original_input,
                revision_changed=result.disposition == "request_optimization",
            )
        strategy = None if result is None else result.strategy
        next_settings = (current_settings.merged(result.execution_settings)
                         if result is not None and result.disposition == "apply"
                         else current_settings)
        unchanged_edit = (strategy is not None and strategy == base
                          and next_settings == current_settings
                          and not (backtest_results and result is not None
                                   and result.run_requested))
        if unchanged_edit:
            strategy = None
        if strategy is not None:
            try:
                if (
                    strategy.catalog != base.catalog or strategy.instrument != base.instrument
                    or strategy.execution != base.execution or strategy.backtest.end > as_of
                    or strategy_requires_events(strategy) or strategy_requires_financials(strategy)
                ):
                    raise ValueError("strategy edit changed a fixed boundary")
                strategy = validate_strategy_against_catalog(strategy, self._catalog)
            except (ValueError, StrategyCatalogError):
                strategy = None
        provenance = None if result is None else result.provenance
        if strategy is not None:
            message = result.message if result is not None else "策略修改已完成。"
            outcome = CompileOutcome(
                status=CompileStatus.READY, strategy=strategy,
                strategy_hash=canonical_hash(strategy), candidate_provenance=provenance,
                is_strategy_edit=True,
                execution_settings=next_settings,
                provenance=(FieldProvenance(path="/", source="bounded_provider/strategy_edit"),),
                run_requested=bool(
                    backtest_results and result is not None and result.run_requested
                ),
                refresh_data=bool(
                    backtest_results and result is not None
                    and result.run_requested and result.refresh_data
                ),
            )
            emit_progress("strategy_ready", "修改后的策略已通过 Catalog 与回测边界校验。")
        else:
            needs_clarification = unchanged_edit or (
                result is not None and result.disposition == "clarify"
            )
            message = (
                "这次修改还没有改变策略。你想调整哪个条件，改成什么？"
                if unchanged_edit else result.message
                if needs_clarification and result is not None else
                "本次修改未完成校验，原策略已保留。请重试这条修改，尚未执行新回测。"
            )
            outcome = CompileOutcome(
                status=CompileStatus.NEEDS_CLARIFICATION, clarification=message,
                diagnostic_code=("strategy_edit_clarification" if needs_clarification
                                 else "strategy_edit_unavailable"),
                revision_base_strategy=base, candidate_provenance=provenance,
                is_strategy_edit=True,
                execution_settings=current_settings,
                pending_edit_run_requested=prior_outcome.pending_edit_run_requested,
                pending_edit_refresh_data=prior_outcome.pending_edit_refresh_data,
            )
        return ClarificationTurnOutcome(
            reply_kind="accepted" if strategy is not None else "clarification",
            assistant_message=message, outcome=outcome,
            compile_input=CompileInput(
                utterance=answer.strip(), instrument_context=base.instrument.symbol,
                as_of_date=as_of,
            ),
            revision_changed=True,
        )

    async def resolve_instrument_context(
        self, value: str, *, require_details: bool = False,
    ) -> str | None:
        """Resolve an instrument-only turn without interpreting strategy text.

        A canonical code is normalised locally.  A name must be proven by the
        server-owned resolver.  Any ambiguity, provider failure or malformed
        result stays unresolved; this method never guesses a security.
        """

        normalized = value.strip()
        if not normalized or len(normalized) > 32:
            return None
        try:
            return normalize_a_share_instrument(normalized).value
        except AshareInstrumentCodeError:
            pass
        if (
            self._instrument_name_resolver is None
            or re.fullmatch(
                r"[\u4e00-\u9fffA-Za-z0-9*STst·\-]{2,32}",
                normalized,
            )
            is None
        ):
            return None
        try:
            resolved = await asyncio.to_thread(self._instrument_name_resolver, normalized)
            return normalize_a_share_instrument(resolved).value
        except (AshareInstrumentCodeError, LookupError, OSError, TimeoutError):
            if require_details:
                raise
            return None

    @property
    def has_clarification_dialogue(self) -> bool:
        return self._clarification_dialogue_router is not None

    async def assess_instrument_clarification(
        self, *, original_input: CompileInput, prior_outcome: CompileOutcome,
        answer: str, pending_label: str | None = None,
        recent_turns: tuple[ClarificationDialogueTurn, ...] = (),
        options: tuple[ClarificationOption, ...] = (),
    ) -> ClarificationDialogueAssessment | None:
        """Interpret a whole stock-confirmation reply, not a name-shaped substring."""
        if self._clarification_dialogue_router is None:
            return None
        return await self._clarification_dialogue_router.assess(ClarificationDialogueRequest(
            answer=answer.strip(), prior_utterance=original_input.utterance,
            diagnostic_code="instrument_confirmation",
            question=prior_outcome.clarification or "想用哪只股票？",
            context_summary=(
                f"已保留交易输入：{original_input.utterance}。当前仅等待用户确认股票。"
                f"此前待确认候选：{pending_label or '未提供'}，候选不代表用户已经选择。"
                "结合最新整句话判断股票选择和是否回测，不能只改写上一轮问题。"
                "用户已经说出想用的股票时不再重复询问旧候选。服务端会核对身份和规则。"
                "只说先别跑表示暂不执行；股票名加先别跑表示确认股票但暂不执行；"
                "取消本次换股才表示不应用新股票。未明确运行意图则保留原有意图。"
                "allowedOptions若非空，列的是已经查实的股票候选，不是策略方向。"
                "用户用编号或代词明确选择时只填对应selected_option_id，"
                "不要把历史股票名伪造成本轮原文instrument_name。"
            ),
            options=options, recent_turns=recent_turns[-20:],
        ))

    async def resolve_unsupported_instrument(
        self, request: CompileInput,
    ) -> tuple[str, CandidateGroundingEvidence] | None:
        """Retain only a verified identity from an unsupported timeframe turn.

        The existing dialogue model extracts the selected name independently
        of DSL grounding, and the security resolver proves it. Identifying a
        stock does not make an intraday strategy executable.
        """

        if (request.instrument_context is not None
                or _unsupported_source_semantics(request.utterance)
                != "non_daily_timeframe_not_supported"):
            return None
        try:
            if self._clarification_dialogue_router is not None:
                assessment = await self._clarification_dialogue_router.assess(
                    ClarificationDialogueRequest(
                        answer=request.utterance, prior_utterance="",
                        diagnostic_code="non_daily_timeframe_not_supported", question="",
                        context_summary=(
                            "身份专用预检：本句已因分钟线执行不受支持而拒绝。"
                            "只提取本轮明确选择使用的唯一股票名称或代码，逐字填写instrument_name"
                            "并标记instrument_selected；未指定、否定或多个不确定股票时不选择。"
                            "保持不支持结论，不改成日线，不生成策略、灵感或选股筛选条件，"
                            "不请求新数据。其余字段不会作为策略或执行指令。"
                        ),
                        options=(),
                        identity_only=True,
                    ),
                )
                name = assessment.instrument_name if assessment is not None else None
                if (assessment is None or not assessment.instrument_selected
                        or name is None or name not in request.utterance):
                    _LOGGER.warning(
                        "unsupported_identity_missing assessment=%s selected=%s name_present=%s "
                        "exact_span=%s",
                        assessment is not None,
                        assessment.instrument_selected if assessment is not None else False,
                        name is not None, name in request.utterance if name is not None else False,
                    )
                    return None
                symbol = await self.resolve_instrument_context(name)
                if symbol is None:
                    _LOGGER.warning("unsupported_identity_unresolved name_length=%s", len(name))
                    return None
                start = request.utterance.index(name)
                return symbol, CandidateGroundingEvidence(
                    path="/instrument/symbol", start=start, end=start + len(name), text=name,
                )
            # Isolated compilers can reuse an already-grounded candidate; the
            # production dialogue path never depends on intraday DSL parsing.
            identities: dict[str, CandidateGroundingEvidence] = {}
            for candidate in await self._generator.generate(request):
                for evidence in candidate.grounding_evidence:
                    if (evidence.path not in {"/instrument/name", "/instrument/symbol"}
                            or not 0 <= evidence.start < evidence.end <= len(request.utterance)
                            or request.utterance[evidence.start:evidence.end] != evidence.text):
                        continue
                    symbol = await self.resolve_instrument_context(evidence.text)
                    if symbol is None:
                        continue
                    if candidate.instrument_symbol is not None:
                        try:
                            candidate_symbol = normalize_a_share_instrument(
                                candidate.instrument_symbol,
                            ).value
                        except AshareInstrumentCodeError:
                            continue
                        if candidate_symbol != symbol:
                            continue
                    identities[symbol] = replace(evidence, path="/instrument/symbol")
            return next(iter(identities.items())) if len(identities) == 1 else None
        except Exception as exc:
            # Identity enrichment is best-effort; preserve the unsupported
            # result even when its provider fails, without logging user data.
            _LOGGER.warning("unsupported_instrument_preflight_unavailable error_type=%s",
                            type(exc).__name__)
            return None

    def _compile_selected_idea_strategy(
        self,
        *,
        request: CompileInput,
        prior_outcome: CompileOutcome,
        proposal: IdeaProposal,
    ) -> CompileOutcome:
        validation_request = CompileInput(
            utterance=request.utterance,
            instrument_context=request.instrument_context,
            as_of_date=self._backtest_anchor_date or request.as_of_date,
        )
        validated = self._validate_direct_idea_strategy(validation_request, proposal)
        candidate_provenance = (
            prior_outcome.candidate_provenance
            or _idea_candidate_provenance(prior_outcome, proposal)
        )
        if validated is None:
            return self._idea_guidance_unavailable(
                candidate_grounding=prior_outcome.candidate_grounding,
                candidate_provenance=candidate_provenance,
            )
        emit_progress("strategy_ready", "所选策略已通过 Catalog 与回测边界校验。")
        return CompileOutcome(
            status=CompileStatus.READY,
            strategy=validated.strategy,
            strategy_hash=validated.strategy_hash,
            provenance=(
                FieldProvenance(path="/", source="bounded_provider/validated_strategy"),
            ),
            candidate_provenance=candidate_provenance,
            candidate_grounding=prior_outcome.candidate_grounding,
            execution_settings=prior_outcome.execution_settings,
        )

    def rebind_current_strategy(
        self, request: CompileInput, prior_outcome: CompileOutcome,
    ) -> CompileOutcome | None:
        base = prior_outcome.strategy or prior_outcome.revision_base_strategy
        if base is None or request.instrument_context is None:
            return None
        strategy = base.model_copy(update={
            "instrument": Instrument(symbol=request.instrument_context),
        })
        validate_strategy_against_catalog(strategy, self._catalog)
        return replace(
            prior_outcome, status=CompileStatus.READY, strategy=strategy,
            strategy_hash=canonical_hash(strategy), clarification=None, diagnostic_code=None,
            revision_base_strategy=None,
            instrument_candidates=(), pending_edit_run_requested=False,
            pending_edit_refresh_data=False, is_strategy_edit=True,
            pending_execution_settings=ExecutionSettingsPatch(),
            provenance=(FieldProvenance(path="/instrument/symbol",
                                        source="dialogue/verified_instrument_change"),),
        )

    def bind_idea_proposal(
        self, request: CompileInput, proposal: IdeaProposal, symbol: str,
    ) -> IdeaProposal | None:
        """Bind and validate one proposal without selecting or executing it."""
        if proposal.strategy_template is None:
            return None
        try:
            instrument = normalize_a_share_instrument(symbol).value
            strategy = proposal.strategy_template.bind(instrument)
        except ValueError:
            return None
        validation_request = replace(
            request, instrument_context=instrument,
            as_of_date=self._backtest_anchor_date or request.as_of_date,
        )
        validated = self._validate_direct_idea_strategy(validation_request, replace(
            proposal, instrument_symbol=instrument, strategy=strategy,
            strategy_hash=canonical_hash(strategy),
        ))
        return None if validated is None else validated.proposal

    def bind_selected_idea(
        self, request: CompileInput, prior_outcome: CompileOutcome,
    ) -> CompileOutcome | None:
        proposal = prior_outcome.selected_idea_proposal
        if (proposal is None and prior_outcome.idea_route is not None
                and request.instrument_context is not None):
            validated: list[IdeaProposal] = []
            for item in prior_outcome.idea_route.proposals:
                if item.strategy_template is None:
                    return None
                if item.instrument_symbol != request.instrument_context:
                    item = replace(item, instrument_name=None, pairing_reason=None)
                current = self.bind_idea_proposal(request, item, request.instrument_context)
                if current is not None:
                    validated.append(current)
            if len(validated) < 2:
                return self._idea_guidance_unavailable()
            return replace(
                prior_outcome,
                clarification="股票已确认。你可以选一个策略方向回测，也可以继续调整规则。",
                idea_route=replace(
                    prior_outcome.idea_route, proposals=tuple(validated),
                    asset_mapping=replace(
                        prior_outcome.idea_route.asset_mapping,
                        instrument_symbol=request.instrument_context,
                        relation="current_page_proxy", evidence_status="host_context_only",
                        rationale="只绑定你刚刚确认的回测股票，不改变已生成的买卖规则。",
                    ),
                ),
            )
        if (proposal is None or proposal.strategy_template is None
                or request.instrument_context is None):
            return None
        if proposal.instrument_symbol != request.instrument_context:
            proposal = replace(proposal, instrument_name=None, pairing_reason=None)
        bound = self.bind_idea_proposal(request, proposal, request.instrument_context)
        if bound is None:
            return self._idea_guidance_unavailable(
                candidate_provenance=prior_outcome.candidate_provenance,
            )
        return self._compile_selected_idea_strategy(
            request=request, prior_outcome=prior_outcome,
            proposal=bound,
        )

    async def answer_idea_data_followup(
        self, *, original_input: CompileInput, prior_outcome: CompileOutcome,
        answer: str, recent_turns: tuple[ClarificationDialogueTurn, ...] = (),
    ) -> ClarificationTurnOutcome | None:
        """Discuss stored candidates before treating a follow-up as a fresh lookup."""
        route = prior_outcome.idea_route
        if (route is None or not route.proposals
                or self._clarification_dialogue_router is None):
            return None
        assessment = await self._assess_clarification_dialogue(
            original_input, prior_outcome, answer, TurnIntent.DATA_QUERY, recent_turns,
            allow_data_query=True,
        )
        if assessment is not None and assessment.requires_new_data:
            return None
        if assessment is not None:
            selection = await self._apply_dialogue_selection(
                original_input, prior_outcome, answer, assessment,
            )
            if selection is not None:
                return selection
        return ClarificationTurnOutcome(
            reply_kind="clarification",
            assistant_message=(assessment.natural_reply if assessment is not None else
                               "对话模型这次未能返回有效回复，请稍后重试。"),
            outcome=prior_outcome, compile_input=original_input, revision_changed=False,
            suggestions=(_rank_clarification_suggestions(
                _clarification_suggestions(prior_outcome), assessment.recommended_option_ids,
            ) if assessment is not None else ()),
        )

    async def answer_clarification(
        self,
        *,
        original_input: CompileInput,
        prior_outcome: CompileOutcome,
        answer: str,
        recent_turns: tuple[ClarificationDialogueTurn, ...] = (),
        dialogue_assessment: ClarificationDialogueAssessment | None = None,
        ready_message: str | None = None,
    ) -> ClarificationTurnOutcome:
        """Continue dialogue; model-authored strategies still pass the DSL gate."""

        if (
            prior_outcome.status is not CompileStatus.NEEDS_CLARIFICATION
            or prior_outcome.diagnostic_code is None
        ):
            raise ValueError("draft revision is not awaiting clarification")
        preserved_instrument_context = _clarification_instrument_context(
            original_input,
            prior_outcome,
        )
        selected_proposal = _selected_clarification_proposal(prior_outcome, answer)
        pragmatic_issue = (
            None if selected_proposal is not None else _clarification_pragmatic_issue(answer)
        )
        turn_intent = (
            TurnIntent.UNKNOWN
            if selected_proposal is not None
            else classify_clarification_turn(answer)
        )
        replace_pending_sentence = turn_intent in {
            TurnIntent.NEW_STRATEGY,
            TurnIntent.VAGUE_STRATEGY,
            TurnIntent.VIEWPOINT,
        }
        continue_pending_viewpoint = (
            prior_outcome.diagnostic_code == "idea_guidance_required"
            and continues_prior_viewpoint(answer)
        )
        # A genuine question, example or rejection still wins.  Otherwise a
        # viewpoint such as "我觉得东方财富会涨" must start fresh instead of
        # being swallowed by the broad conversational-language detector.
        if replace_pending_sentence and pragmatic_issue == "conversation":
            pragmatic_issue = None
        if turn_intent is TurnIntent.CASUAL:
            pragmatic_issue = "conversation"
        elif turn_intent is TurnIntent.CANCEL:
            pragmatic_issue = "negation"
        # An unknown/casual follow-up needs a contextual model decision before
        # standalone compilation. Otherwise a newly generated idea can be
        # discarded as "still needs clarification" and generated a second time.
        assessed_early = dialogue_assessment is not None or (
            selected_proposal is None and turn_intent in {
                TurnIntent.UNKNOWN, TurnIntent.CASUAL, TurnIntent.SELECT_OPTION,
            }
        )
        assessment = dialogue_assessment
        if assessed_early:
            if assessment is None:
                assessment = await self._assess_clarification_dialogue(
                    original_input, prior_outcome, answer, turn_intent, recent_turns,
                )
            if assessment is not None:
                selection = await self._apply_dialogue_selection(
                    original_input, prior_outcome, answer, assessment,
                )
                if selection is not None:
                    return selection
                inspiration = await self._compile_dialogue_inspiration(
                    request=CompileInput(
                        utterance=answer.strip(),
                        instrument_context=preserved_instrument_context,
                        as_of_date=self._backtest_anchor_date or original_input.as_of_date,
                    ),
                    assessment=assessment, recent_turns=recent_turns,
                )
                if inspiration is not None:
                    inspiration_input, inspiration_outcome = inspiration
                    return ClarificationTurnOutcome(
                        reply_kind="accepted",
                        assistant_message=(
                            inspiration_outcome.clarification or assessment.natural_reply
                        ),
                        outcome=inspiration_outcome, compile_input=inspiration_input,
                        revision_changed=True,
                    )
                if assessment.reply_kind in {"question", "off_topic", "cancelled"}:
                    return ClarificationTurnOutcome(
                        reply_kind="clarification", assistant_message=assessment.natural_reply,
                        outcome=prior_outcome, compile_input=original_input,
                        revision_changed=False,
                    )
            elif self._clarification_dialogue_router is not None:
                return ClarificationTurnOutcome(
                    reply_kind="clarification",
                    assistant_message="对话模型这次未能返回有效回复，请稍后重试。",
                    outcome=prior_outcome, compile_input=original_input,
                    revision_changed=False,
                )
        accepted_as_replacement = False
        accepted_as_continuation = False
        if selected_proposal is not None:
            merged_input = CompileInput(
                utterance=selected_proposal.suggested_utterance,
                instrument_context=(
                    selected_proposal.instrument_symbol or preserved_instrument_context
                ),
                as_of_date=original_input.as_of_date,
            )
            if selected_proposal.strategy is not None:
                recompiled = self._compile_selected_idea_strategy(
                    request=merged_input,
                    prior_outcome=prior_outcome,
                    proposal=selected_proposal,
                )
            elif merged_input.instrument_context is None:
                # Selecting a model-authored direction does not supply the
                # missing security. Preserve that exact direction for the next
                # turn without calling either the strategy model or research
                # route again; the normal instrument-supplement path will
                # compile it once the user provides a proven A-share identity.
                recompiled = CompileOutcome(
                    status=CompileStatus.NEEDS_CLARIFICATION,
                    clarification="请告诉我想回测哪一只 A 股（股票名称或 6 位代码）。",
                    diagnostic_code="instrument_required",
                    selected_idea_proposal=selected_proposal,
                    candidate_provenance=_idea_candidate_provenance(
                        prior_outcome,
                        selected_proposal,
                    ),
                    candidate_grounding=prior_outcome.candidate_grounding,
                    execution_settings=prior_outcome.execution_settings,
                )
            else:
                # Legacy/local proposal routes still use their existing
                # deterministic compile path. Direct-DSL model routes never
                # reinterpret model-authored text.
                recompiled = await self.compile(merged_input)
            accepted_as_replacement = True
        elif continue_pending_viewpoint:
            merged_input = CompileInput(
                utterance=_merge_viewpoint_continuation(
                    original_input.utterance,
                    answer,
                ),
                instrument_context=preserved_instrument_context,
                as_of_date=original_input.as_of_date,
            )
            # A continued viewpoint is one semantic turn.  Compile the merged
            # sentence exactly once instead of first treating the new text as
            # a missing-slot answer and then retrying several fallback paths.
            recompiled = await self.compile(merged_input)
            accepted_as_continuation = True
        elif turn_intent is TurnIntent.SUPPLEMENT and pragmatic_issue in {None, "negation"}:
            # Interpret a custom slot answer with the user's existing rules first.
            # Compiling the isolated half-rule needlessly generates fresh choices
            # and can lose the opposite side. The model still receives the text,
            # including rejections, and all source/Catalog validation still runs.
            merged_input = CompileInput(
                utterance=_merge_clarification_answer(
                    original_input.utterance, answer,
                    diagnostic_code=prior_outcome.diagnostic_code,
                ),
                instrument_context=preserved_instrument_context,
                as_of_date=original_input.as_of_date,
            )
            recompiled = await self.compile(merged_input)
            accepted_as_continuation = recompiled.status is CompileStatus.READY
        elif pragmatic_issue is None:
            standalone_input = CompileInput(
                utterance=answer.strip(),
                instrument_context=preserved_instrument_context,
                as_of_date=original_input.as_of_date,
            )
            standalone_outcome = await self.compile(standalone_input)
            if (
                standalone_outcome.diagnostic_code == "instrument_context_mismatch"
                and replace_pending_sentence
                and preserved_instrument_context is not None
            ):
                # A full new sentence may explicitly name a different stock.
                # Retry without the stale page context; the normal resolver
                # still has to prove the new identity.
                standalone_input = CompileInput(
                    utterance=answer.strip(),
                    instrument_context=None,
                    as_of_date=original_input.as_of_date,
                )
                standalone_outcome = await self.compile(standalone_input)
            if standalone_outcome.status is CompileStatus.READY or (
                replace_pending_sentence
                and standalone_outcome.status is CompileStatus.NEEDS_CLARIFICATION
            ):
                merged_input = standalone_input
                recompiled = standalone_outcome
                accepted_as_replacement = True
            else:
                merged_input = CompileInput(
                    utterance=_merge_clarification_answer(
                        original_input.utterance,
                        answer,
                        diagnostic_code=prior_outcome.diagnostic_code,
                    ),
                    instrument_context=preserved_instrument_context,
                    as_of_date=original_input.as_of_date,
                )
                recompiled = await self.compile(merged_input)
                if (
                    recompiled.status is not CompileStatus.READY
                    and prior_outcome.diagnostic_code in _INSTRUMENT_CLARIFICATION_CODES
                    and standalone_outcome.status is CompileStatus.NEEDS_CLARIFICATION
                    and standalone_outcome.diagnostic_code not in _INSTRUMENT_CLARIFICATION_CODES
                    and _has_grounded_instrument(standalone_outcome)
                ):
                    merged_input = standalone_input
                    recompiled = standalone_outcome
                    accepted_as_replacement = True
        else:
            merged_input = original_input
            recompiled = prior_outcome
        progressed = (
            accepted_as_replacement
            or accepted_as_continuation
            or recompiled.status is CompileStatus.READY
            or (
                recompiled.status is CompileStatus.NEEDS_CLARIFICATION
                and recompiled.diagnostic_code != prior_outcome.diagnostic_code
            )
        )
        if progressed:
            if recompiled.status is CompileStatus.READY:
                message = ready_message or await self.compose_ready_response(
                    answer=answer, outcome=recompiled, recent_turns=recent_turns,
                )
            else:
                next_question = recompiled.clarification or "还需要再补充一项信息。"
                message = (next_question if recompiled.idea_route is not None else
                           await self.compose_dialogue_response(
                               answer=answer, question=next_question,
                               context=f"当前交易想法：{merged_input.utterance}",
                               recent_turns=recent_turns,
                           ))
            return ClarificationTurnOutcome(
                reply_kind="accepted",
                assistant_message=message,
                outcome=recompiled,
                compile_input=merged_input,
                revision_changed=True,
                suggestions=_clarification_suggestions(recompiled),
            )

        if recompiled.status in {CompileStatus.UNSUPPORTED, CompileStatus.INVALID}:
            pragmatic_issue = "unsupported"

        suggestions = _clarification_suggestions(prior_outcome)
        context_summary = (
            prior_outcome.idea_route.understanding
            if prior_outcome.idea_route is not None
            else "你刚才已经说清的部分我会原样保留。"
        ).rstrip("。！？!? ")
        if not assessed_early:
            assessment = await self._assess_clarification_dialogue(
                original_input, prior_outcome, answer, turn_intent, recent_turns,
            )
        if assessment is not None:
            inspiration = await self._compile_dialogue_inspiration(
                request=CompileInput(
                    utterance=answer.strip(), instrument_context=preserved_instrument_context,
                    as_of_date=self._backtest_anchor_date or original_input.as_of_date,
                ),
                assessment=assessment, recent_turns=recent_turns,
            )
            if inspiration is not None:
                inspiration_input, inspiration_outcome = inspiration
                return ClarificationTurnOutcome(
                    reply_kind="accepted",
                    assistant_message=inspiration_outcome.clarification or assessment.natural_reply,
                    outcome=inspiration_outcome, compile_input=inspiration_input,
                    revision_changed=True,
                )
            suggestions = _rank_clarification_suggestions(
                suggestions,
                assessment.recommended_option_ids,
            )
            message = assessment.natural_reply
            if assessment.reply_kind in {"off_topic", "cancelled"}:
                suggestions = ()
        elif self._clarification_dialogue_router is not None:
            message = "对话模型这次未能返回有效回复，请稍后重试。"
            suggestions = ()
        else:
            acknowledgement = _pragmatic_fallback(pragmatic_issue)
            next_action = _textual_suggestion_hint(suggestions)
            message = (
                f"{acknowledgement}{context_summary}。"
                f"{_clarification_followup(prior_outcome.clarification)}"
                f"{next_action}"
            )
        return ClarificationTurnOutcome(
            reply_kind="clarification",
            assistant_message=message,
            outcome=prior_outcome,
            compile_input=original_input,
            revision_changed=False,
            suggestions=suggestions,
        )

    async def compose_dialogue_response(
        self, *, answer: str, question: str, context: str,
        recent_turns: tuple[ClarificationDialogueTurn, ...] = (),
        verified_instruments: tuple[tuple[str, str], ...] = (),
    ) -> str:
        """Render a whole response from verified state, without changing that state."""
        if self._clarification_dialogue_router is None:
            return question
        assessment = await self._clarification_dialogue_router.assess(
            ClarificationDialogueRequest(
                answer=answer, prior_utterance="", diagnostic_code="response_only",
                question=question, context_summary=context, options=(),
                recent_turns=recent_turns[-20:], response_only=True,
                verified_instruments=verified_instruments,
            )
        )
        return (assessment.natural_reply if assessment is not None else
                "对话模型这次未能返回有效回复，请稍后重试。")

    async def compose_ready_response(
        self, *, answer: str, outcome: CompileOutcome,
        recent_turns: tuple[ClarificationDialogueTurn, ...] = (),
    ) -> str:
        """Confirm verified rules without templating prose or authorizing a run."""
        assert outcome.status is CompileStatus.READY and outcome.strategy is not None
        return await self.compose_dialogue_response(
            answer=answer,
            question="买卖规则已准备好，可以核对。",
            context=(
                "本次规则已通过校验，下面是当前实际策略及成交设置。只用一句简短中文承接"
                "本轮输入，确认规则已准备好；不全文复述规则，不追加缺项问题。"
                "本步骤只准备规则，没有产生回测结果；不能承诺收益或声称已经执行回测。"
                f"策略：{outcome.strategy.model_dump_json()}；"
                f"成交设置：{outcome.execution_settings.model_dump_json()}"
            ),
            recent_turns=recent_turns,
        )

    async def compile(self, request: CompileInput) -> CompileOutcome:
        if not request.utterance.strip():
            return CompileOutcome(
                status=CompileStatus.INVALID,
                diagnostic_code="empty_utterance",
            )
        if len(request.utterance) > 2_000:
            return CompileOutcome(
                status=CompileStatus.INVALID,
                diagnostic_code="utterance_too_long",
            )
        if request.as_of_date > self._trusted_date_provider():
            return CompileOutcome(
                status=CompileStatus.INVALID,
                diagnostic_code="request_as_of_date_in_future",
            )
        effective_as_of_date = self._backtest_anchor_date or request.as_of_date
        effective_request = (
            request
            if effective_as_of_date == request.as_of_date
            else replace(request, as_of_date=effective_as_of_date)
        )
        source_semantic_diagnostic = _unsupported_source_semantics(request.utterance)
        candidates: tuple[CandidateAst, ...] | None = None
        if source_semantic_diagnostic == "ambiguous_boolean_expression":
            # Counting every word "buy" confuses stock intentions with conditions.
            # A bounded model candidate has already passed explicit-leaf coverage
            # and connector grounding, so prefer that evidence over this heuristic.
            candidates = await self._generator.generate(effective_request)
            if candidates and all(
                item.provenance is not None and item.provenance.source == "bounded_provider"
                and item.unsupported_code in {
                    None, "entry_rule_not_recognized", "exit_rule_not_recognized",
                } for item in candidates
            ):
                source_semantic_diagnostic = None
        if source_semantic_diagnostic is not None:
            if source_semantic_diagnostic in _LOCAL_CLARIFICATION_ROUTE_CODES:
                clarification_request = effective_request
                candidate_grounding: tuple[CandidateGroundingEvidence, ...] = ()
                candidate_provenance: CandidateProvenance | None = None
                identity_candidate: CandidateAst | None = None
                if effective_request.instrument_context is None:
                    # Source-level ambiguity is checked before strategy
                    # translation, but a trusted name resolver may still have
                    # enough information to identify the A-share.  Run the
                    # existing generator only as an identity preflight and
                    # discard every strategy field it returns.  This lets a
                    # sentence such as ``东方财富OBV变化时买入`` ask
                    # about OBV direction instead of forgetting the stock.
                    identity_candidates = (candidates if candidates is not None
                                           else await self._generator.generate(effective_request))
                    if identity_candidates:
                        identity_candidate = identity_candidates[0]
                        if _candidate_instrument_is_grounded(
                            identity_candidate,
                            effective_request.utterance,
                        ):
                            assert identity_candidate.instrument_symbol is not None
                            clarification_request = CompileInput(
                                utterance=effective_request.utterance,
                                instrument_context=normalize_a_share_instrument(
                                    identity_candidate.instrument_symbol
                                ).value,
                                as_of_date=effective_request.as_of_date,
                            )
                            candidate_grounding = identity_candidate.grounding_evidence
                            candidate_provenance = identity_candidate.provenance
                if self._idea_router is not None:
                    idea_outcome = await self._compile_idea_guidance(
                        clarification_request, known_candidate=identity_candidate,
                    )
                    if idea_outcome is not None:
                        return idea_outcome
                    return self._idea_guidance_unavailable(
                        candidate_grounding=candidate_grounding,
                        candidate_provenance=candidate_provenance,
                    )
                clarification_outcome = await self._compile_local_clarification(
                    clarification_request,
                    source_semantic_diagnostic,
                    candidate_grounding=candidate_grounding,
                    candidate_provenance=candidate_provenance,
                )
                if clarification_outcome is not None:
                    return clarification_outcome
            return CompileOutcome(
                status=(
                    CompileStatus.NEEDS_CLARIFICATION
                    if source_semantic_diagnostic == "indicator_trigger_requires_clarification"
                    else CompileStatus.UNSUPPORTED
                ),
                clarification=_SOURCE_SEMANTIC_EXPLANATIONS.get(source_semantic_diagnostic),
                diagnostic_code=source_semantic_diagnostic,
            )
        # "不卖出 / 暂不卖出 / 没有卖出" explicitly leaves the exit slot
        # open.  Preserve the recognised entry and offer only compiler-gated
        # exit choices; never let the literal 卖出 token make the rule look
        # complete or let a generic negation error hide the useful entry.  The
        # source-semantic gate above still wins for unsupported timing or
        # execution instructions.
        if has_explicitly_missing_exit(effective_request.utterance):
            if self._idea_router is not None:
                idea_outcome = await self._compile_idea_guidance(effective_request)
                if idea_outcome is not None:
                    return idea_outcome
                return self._idea_guidance_unavailable()
            missing_exit = await self._compile_local_clarification(
                effective_request,
                "exit_rule_not_recognized",
            )
            if missing_exit is not None:
                return missing_exit
        initial_conversation = await self._compile_initial_conversation(effective_request)
        if initial_conversation is not None:
            return initial_conversation
        if (
            classify_clarification_turn(effective_request.utterance)
            is TurnIntent.VAGUE_STRATEGY
        ):
            # Familiar intuitions such as "低买高卖" are useful hypotheses,
            # but not executable rules.  Prefer the configured model route;
            # every returned sentence is still recompiled and Catalog-gated.
            if self._idea_router is not None:
                idea_outcome = await self._compile_idea_guidance(effective_request)
                if idea_outcome is not None:
                    return idea_outcome
                return self._idea_guidance_unavailable()
            # Isolated compilers without an idea router retain the local
            # guidance fixtures used by deterministic unit tests.
            vague_guidance = await self._compile_local_clarification(
                effective_request,
                "no_supported_signal_recognized",
            )
            if vague_guidance is not None:
                return vague_guidance
            if effective_request.instrument_context is None:
                return CompileOutcome(
                    status=CompileStatus.NEEDS_CLARIFICATION,
                    clarification="先告诉我想回测哪一只 A 股，我再给你 3 种可执行的细化方式。",
                    diagnostic_code="instrument_required",
                )
            return CompileOutcome(
                status=CompileStatus.NEEDS_CLARIFICATION,
                clarification=(
                    "这句话表达了交易方向，但还没有可执行的买卖条件。"
                    "请补充指标、触发方向和卖出规则。"
                ),
                diagnostic_code="vague_strategy_requires_clarification",
            )
        if self._idea_router is not None and _looks_like_broad_viewpoint(
            effective_request.utterance
        ):
            idea_outcome = await self._compile_idea_guidance(effective_request)
            if idea_outcome is not None:
                return idea_outcome
            return self._idea_guidance_unavailable()
        if candidates is None:
            candidates = await self._generator.generate(effective_request)
        if not candidates:
            return CompileOutcome(
                status=CompileStatus.UNSUPPORTED,
                diagnostic_code="no_candidate_generated",
            )
        if self._idea_router is not None and all(
            item.unsupported_code in _IDEA_ROUTE_DIAGNOSTIC_CODES for item in candidates
        ):
            idea_outcome = await self._compile_idea_guidance(
                effective_request, known_candidate=candidates[0],
            )
            if idea_outcome is not None:
                return idea_outcome
            return self._idea_guidance_unavailable(
                candidate_grounding=candidates[0].grounding_evidence,
                candidate_provenance=candidates[0].provenance,
            )
        candidate = candidates[0]
        if len(candidates) == 1 and candidate.unsupported_code is not None:
            if candidate.unsupported_code in _LOCAL_CLARIFICATION_ROUTE_CODES:
                clarification_request = effective_request
                if (
                    clarification_request.instrument_context is None
                    and candidate.instrument_symbol is not None
                ):
                    # A standalone company name may already have been resolved
                    # by the server-owned security master.  Reuse that trusted
                    # symbol while validating clarification proposals instead
                    # of falling back to a generic text prompt.
                    clarification_request = CompileInput(
                        utterance=effective_request.utterance,
                        instrument_context=candidate.instrument_symbol,
                        as_of_date=effective_request.as_of_date,
                    )
                if self._idea_router is not None:
                    idea_outcome = await self._compile_idea_guidance(
                        clarification_request, known_candidate=candidate,
                    )
                    if idea_outcome is not None:
                        return idea_outcome
                    return self._idea_guidance_unavailable(
                        candidate_grounding=candidate.grounding_evidence,
                        candidate_provenance=candidate.provenance,
                    )
                clarification_outcome = await self._compile_local_clarification(
                    clarification_request,
                    candidate.unsupported_code,
                    candidate_grounding=candidate.grounding_evidence,
                    candidate_provenance=candidate.provenance,
                )
                if clarification_outcome is not None:
                    return replace(clarification_outcome,
                                   execution_settings=candidate.execution_settings)
            if candidate.unsupported_code == "return_period_requires_clarification":
                return CompileOutcome(
                    status=CompileStatus.NEEDS_CLARIFICATION,
                    clarification=(
                        "你说了涨跌幅或收益率，但没有说明观察周期。请明确是当日、"
                        "还是几日涨跌幅；系统不会默认为 5 日。"
                    ),
                    diagnostic_code=candidate.unsupported_code,
                    candidate_provenance=candidate.provenance,
                    candidate_grounding=candidate.grounding_evidence,
                    execution_settings=candidate.execution_settings,
                )
            if candidate.unsupported_code == "natural_day_holding_period_requires_clarification":
                return CompileOutcome(
                    status=CompileStatus.NEEDS_CLARIFICATION,
                    clarification=(
                        "你说的是自然日。请一次确认卖出时点：到期后的下一可交易日开盘，"
                        "还是改按买入成交后的交易日计数？"
                    ),
                    diagnostic_code=candidate.unsupported_code,
                    candidate_provenance=candidate.provenance,
                    candidate_grounding=candidate.grounding_evidence,
                    execution_settings=candidate.execution_settings,
                )
            if candidate.unsupported_code == "candidate_provider_low_confidence":
                return CompileOutcome(
                    status=CompileStatus.NEEDS_CLARIFICATION,
                    clarification=(
                        "我还没有完全理解这条交易规则。为了准确还原你的想法，"
                        "麻烦在一句话里说明：什么条件买入、什么条件卖出。"
                        "我会严格按你提供的条件进行回测，不擅自补充默认策略。"
                    ),
                    diagnostic_code=candidate.unsupported_code,
                    candidate_provenance=candidate.provenance,
                    candidate_grounding=candidate.grounding_evidence,
                    execution_settings=candidate.execution_settings,
                )
            if candidate.unsupported_code in {
                "entry_rule_not_recognized",
                "exit_rule_not_recognized",
                "strategy_rule_incomplete",
            }:
                if candidate.unsupported_code == "strategy_rule_incomplete":
                    clarification = (
                        "我识别到了指标或事件，但还缺完整的买卖规则。"
                        "请在原话中一次补充什么时候买入、什么时候卖出；"
                        "系统不会替你生成默认交易策略。"
                    )
                elif candidate.unsupported_code == "entry_rule_not_recognized":
                    clarification = (
                        "已识别卖出条件。你想在什么条件下买入？"
                        "请在原话中补充明确的买入条件；系统不会替你补默认买入规则。"
                    )
                else:
                    clarification = (
                        "已识别买入条件。你想在什么条件下卖出？"
                        "请在原话中补充明确的卖出条件；系统不会替你补默认卖出规则。"
                    )
                return CompileOutcome(
                    status=CompileStatus.NEEDS_CLARIFICATION,
                    clarification=clarification,
                    diagnostic_code=candidate.unsupported_code,
                    candidate_provenance=candidate.provenance,
                    candidate_grounding=candidate.grounding_evidence,
                    execution_settings=candidate.execution_settings,
                )
            return CompileOutcome(
                status=CompileStatus.UNSUPPORTED,
                diagnostic_code=candidate.unsupported_code,
                candidate_provenance=candidate.provenance,
                candidate_grounding=candidate.grounding_evidence,
            )
        rejections: list[CandidateRejectionSummary] = []
        valid_candidates: list[tuple[int, CandidateAst, StrategySpec, str]] = []
        for candidate_rank, current in enumerate(candidates[:3], start=1):
            rejection_code: str | None = None
            if current.unsupported_code is not None:
                rejection_code = current.unsupported_code
            elif current.instrument_symbol is None:
                rejection_code = "instrument_required"
            else:
                try:
                    instrument_symbol = normalize_a_share_instrument(
                        current.instrument_symbol
                    ).value
                except AshareInstrumentCodeError:
                    rejection_code = "invalid_a_share_instrument"
                else:
                    rejection_code = _period_error(current, effective_as_of_date)
                    if rejection_code is None:
                        try:
                            current_strategy = self._build_strategy(
                                current,
                                effective_as_of_date,
                                instrument_symbol=instrument_symbol,
                            )
                            validate_strategy_against_catalog(current_strategy, self._catalog)
                        except _UnsupportedCandidateSemantics as exc:
                            rejection_code = exc.diagnostic_code
                        except (ValueError, StrategyCatalogError) as exc:
                            rejection_code = f"strategy_validation_failed:{type(exc).__name__}"
                        else:
                            valid_candidates.append(
                                (
                                    candidate_rank,
                                    current,
                                    current_strategy,
                                    canonical_hash(current_strategy),
                                )
                            )
                            continue
            assert rejection_code is not None
            rejections.append(
                CandidateRejectionSummary(
                    candidate_rank=candidate_rank,
                    diagnostic_code=rejection_code,
                )
            )

        if not valid_candidates:
            first_code = rejections[0].diagnostic_code
            first_provenance = candidates[0].provenance
            if all(
                item.diagnostic_code == POSITION_AWARE_EXIT_AND_UNSUPPORTED for item in rejections
            ):
                return CompileOutcome(
                    status=CompileStatus.UNSUPPORTED,
                    clarification=_POSITION_AWARE_EXIT_AND_EXPLANATION,
                    diagnostic_code=POSITION_AWARE_EXIT_AND_UNSUPPORTED,
                    candidate_provenance=first_provenance,
                    candidate_grounding=candidates[0].grounding_evidence,
                    candidate_rejections=tuple(rejections),
                )
            if all(item.diagnostic_code == "instrument_required" for item in rejections):
                return CompileOutcome(
                    status=CompileStatus.NEEDS_CLARIFICATION,
                    clarification="请告诉我想回测哪一只 A 股（股票名称或 6 位代码）。",
                    diagnostic_code="instrument_required",
                    candidate_provenance=first_provenance,
                    candidate_grounding=candidates[0].grounding_evidence,
                    candidate_rejections=tuple(rejections),
                    selected_idea_proposal=self._unbound_candidate_proposal(
                        effective_request, candidates[0],
                    ),
                    execution_settings=candidates[0].execution_settings,
                    instrument_suggestion_declined=candidates[0].instrument_suggestion_declined,
                )
            if all(item.diagnostic_code == "invalid_a_share_instrument" for item in rejections):
                return CompileOutcome(
                    status=CompileStatus.UNSUPPORTED,
                    diagnostic_code="invalid_a_share_instrument",
                    candidate_provenance=first_provenance,
                    candidate_grounding=candidates[0].grounding_evidence,
                    candidate_rejections=tuple(rejections),
                )
            if len(candidates) == 1 and first_provenance is None:
                if first_code.startswith("strategy_validation_failed:"):
                    return CompileOutcome(
                        status=CompileStatus.INVALID,
                        diagnostic_code=first_code,
                        candidate_rejections=tuple(rejections),
                    )
                return CompileOutcome(
                    status=CompileStatus.UNSUPPORTED,
                    diagnostic_code=first_code,
                    candidate_rejections=tuple(rejections),
                )
            return CompileOutcome(
                status=CompileStatus.NEEDS_CLARIFICATION,
                clarification=(
                    "我已尝试按原话还原候选，但没有一个通过当前指标、事件和日期门禁。"
                    "请一次写清买入条件、卖出条件和回测区间；系统不会执行未通过校验的候选。"
                ),
                diagnostic_code="candidate_batch_no_valid_strategy",
                candidate_provenance=first_provenance,
                candidate_grounding=candidates[0].grounding_evidence,
                candidate_rejections=tuple(rejections),
            )

        unique_valid: dict[str, tuple[int, CandidateAst, StrategySpec, str]] = {}
        for item in valid_candidates:
            unique_valid.setdefault(item[3], item)
        alternatives = tuple(
            CandidateAlternativeSummary(candidate_rank=item[0], strategy_hash=item[3])
            for item in unique_valid.values()
        )
        if len(unique_valid) > 1:
            first_valid = valid_candidates[0][1]
            return CompileOutcome(
                status=CompileStatus.NEEDS_CLARIFICATION,
                clarification=(
                    "这句话能还原成多个不同且都合法的交易策略。请一次明确你要的"
                    "指标、触发方向和参数；系统不会按模型置信度替你静默选择。"
                ),
                diagnostic_code="candidate_set_ambiguous",
                candidate_provenance=first_valid.provenance,
                candidate_grounding=first_valid.grounding_evidence,
                candidate_rejections=tuple(rejections),
                candidate_alternatives=alternatives,
            )

        _candidate_rank, candidate, strategy, strategy_hash = next(iter(unique_valid.values()))

        start_source, end_source = _period_provenance(
            candidate,
            snapshot_anchored=self._backtest_anchor_date is not None,
        )
        provenance = [
            FieldProvenance(path="/instrument/symbol", source="utterance_or_stock_context"),
            FieldProvenance(path="/backtest/start", source=start_source),
            FieldProvenance(path="/backtest/end", source=end_source),
            FieldProvenance(
                path="/backtest/initial_cash_cny",
                source=(
                    "utterance/explicit_initial_cash"
                    if candidate.initial_cash_cny is not None
                    else "default/initial_cash"
                ),
            ),
        ]
        provenance.extend(
            FieldProvenance(path=path, source="default/catalog_policy")
            for path in candidate.defaulted_fields
        )
        return CompileOutcome(
            status=CompileStatus.READY,
            strategy=strategy,
            strategy_hash=strategy_hash,
            provenance=tuple(sorted(provenance, key=lambda item: item.path)),
            candidate_provenance=candidate.provenance,
            candidate_grounding=candidate.grounding_evidence,
            candidate_rejections=tuple(rejections),
            candidate_alternatives=alternatives,
            execution_settings=candidate.execution_settings,
        )

    async def _compile_initial_conversation(
        self,
        request: CompileInput,
    ) -> CompileOutcome | None:
        """Route greetings or creative inspiration without authorizing execution."""

        if not _looks_like_initial_conversation(request.utterance):
            return None
        assessment = None
        if self._clarification_dialogue_router is not None:
            assessment = await self._clarification_dialogue_router.assess(
                ClarificationDialogueRequest(
                    answer=request.utterance.strip(),
                    prior_utterance="",
                    diagnostic_code="conversation_only",
                    question="",
                    context_summary="这是首次对话；可把人物或比喻作为待确认的策略创作灵感。",
                    options=(),
                )
            )
        if assessment is not None:
            inspiration = await self._compile_dialogue_inspiration(
                request=request, assessment=assessment,
            )
            if inspiration is not None:
                return inspiration[1]
        if assessment is None and self._clarification_dialogue_router is not None:
            return CompileOutcome(
                status=CompileStatus.NEEDS_CLARIFICATION,
                clarification="对话模型这次未能返回有效回复，请稍后重试。",
                diagnostic_code="dialogue_model_unavailable",
            )
        return CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION,
            clarification=(assessment.natural_reply if assessment is not None
                           else "你好，可以说说你想研究的交易想法。"),
            diagnostic_code="conversation_only",
        )

    async def _assess_clarification_dialogue(
        self, original_input: CompileInput, prior_outcome: CompileOutcome,
        answer: str, turn_intent: TurnIntent,
        recent_turns: tuple[ClarificationDialogueTurn, ...],
        *, allow_data_query: bool = False,
    ) -> ClarificationDialogueAssessment | None:
        if self._clarification_dialogue_router is None:
            return None
        route = prior_outcome.idea_route
        context_summary = (
            route.understanding if route is not None
            else ("之前只是普通对话，没有待补充的交易条件。"
                  if prior_outcome.diagnostic_code == "conversation_only"
                  else f"此前交易输入：{original_input.utterance}")
        ).rstrip("。！？!? ")
        if route is not None:
            candidate_evidence = "\n".join(
                f"{item.id}：{item.instrument_name or '名称未提供'}"
                f"（{item.instrument_symbol}）；此前匹配依据：{item.pairing_reason or '未提供'}"
                for item in route.proposals if item.instrument_symbol is not None
            )
            if candidate_evidence:
                context_summary += (
                    f"\n已存候选及当时的依据（不代表回测结果）：\n{candidate_evidence}"
                )
        return await self._clarification_dialogue_router.assess(
            ClarificationDialogueRequest(
                answer=answer.strip(), prior_utterance=original_input.utterance,
                diagnostic_code=prior_outcome.diagnostic_code or "strategy_rule_incomplete",
                question=("" if turn_intent is TurnIntent.CASUAL or
                          prior_outcome.diagnostic_code in {
                              "conversation_only", "dialogue_model_unavailable",
                          } else
                          prior_outcome.clarification or "请补充缺失的策略条件。"),
                context_summary=context_summary,
                options=tuple(
                    ClarificationOption(id=item.id, title=item.title, preview=item.preview)
                    for item in _clarification_suggestions(prior_outcome)
                ),
                recent_turns=recent_turns[-20:],
                research=(prior_outcome.idea_route.research
                          if prior_outcome.idea_route is not None else None),
                allow_data_query=allow_data_query,
            )
        )

    async def _apply_dialogue_selection(
        self, request: CompileInput, prior: CompileOutcome, answer: str,
        assessment: ClarificationDialogueAssessment,
    ) -> ClarificationTurnOutcome | None:
        """Apply the model's explicit choice to existing, validated templates."""
        if prior.idea_route is None or assessment.strategy_inspiration is not None:
            return None
        outcome = prior
        name = assessment.instrument_name
        if assessment.instrument_selected and name is not None and name in answer:
            instrument = await self.resolve_instrument_context(name)
            if instrument is None:
                return ClarificationTurnOutcome(
                    reply_kind="clarification",
                    assistant_message=f"这次还没能核实{name}，请补充股票代码；原方案先保留。",
                    outcome=prior, compile_input=request, revision_changed=False,
                )
            request = replace(request, instrument_context=instrument)
            bound = self.bind_selected_idea(request, prior)
            if bound is None:
                return None
            start = answer.index(name)
            outcome = replace(bound, candidate_grounding=(CandidateGroundingEvidence(
                path="/instrument/symbol", start=start, end=start + len(name), text=name,
            ),))
            if outcome.status is CompileStatus.READY:
                # Binding the already selected template consumes the selection;
                # a proposal id from the old route must not trigger regeneration.
                return ClarificationTurnOutcome(
                    reply_kind="accepted", assistant_message=assessment.natural_reply,
                    outcome=outcome, compile_input=request, revision_changed=True,
                )
            if outcome.idea_route is not None:
                outcome = replace(outcome, idea_route=replace(
                    outcome.idea_route, proposals=tuple(replace(
                        item, instrument_name=name,
                    ) for item in outcome.idea_route.proposals),
                ))
        if assessment.selected_option_id is not None:
            # Reuse the exact same typed-selection path as a chip click. No
            # regeneration of a similarly named strategy and no extra model call.
            selected = _selected_clarification_proposal(outcome, assessment.selected_option_id)
            if selected is None:
                return None
            turn = await self.answer_clarification(
                original_input=request, prior_outcome=outcome, answer=selected.id,
                ready_message=assessment.natural_reply,
            )
            return turn
        if outcome is prior:
            return None
        if outcome.status is CompileStatus.NEEDS_CLARIFICATION:
            outcome = replace(outcome, clarification=assessment.natural_reply)
        return ClarificationTurnOutcome(
            reply_kind="accepted", assistant_message=assessment.natural_reply,
            outcome=outcome, compile_input=request, revision_changed=True,
            suggestions=_clarification_suggestions(outcome),
        )

    async def _compile_dialogue_inspiration(
        self, *, request: CompileInput, assessment: ClarificationDialogueAssessment,
        recent_turns: tuple[ClarificationDialogueTurn, ...] = (),
    ) -> tuple[CompileInput, CompileOutcome] | None:
        if assessment.strategy_inspiration is None or self._idea_router is None:
            return None
        instrument = request.instrument_context
        name = assessment.instrument_name
        if name is not None:
            # Extraction is model-authored; identity still needs a real resolver.
            if name not in request.utterance:
                return None
            instrument = await self.resolve_instrument_context(name)
        inspiration_input = replace(
            request, instrument_context=instrument,
            idea_inspiration=assessment.strategy_inspiration,
            idea_context=tuple(
                f"用户：{turn.user_text}\n助手：{turn.assistant_text}"
                for turn in recent_turns[-20:]
            ),
        )
        outcome = await self._compile_idea_guidance(inspiration_input)
        if outcome is None:
            outcome = self._idea_guidance_unavailable()
        if instrument is not None and name is not None:
            start = request.utterance.index(name)
            outcome = replace(outcome, candidate_grounding=(CandidateGroundingEvidence(
                path="/instrument/symbol", start=start, end=start + len(name), text=name,
            ),))
        return inspiration_input, outcome

    async def _compile_idea_guidance(
        self,
        request: CompileInput,
        *, known_candidate: CandidateAst | None = None,
    ) -> CompileOutcome | None:
        if self._idea_router is None:
            return None
        routed_request = request
        viewpoint_grounding: tuple[CandidateGroundingEvidence, ...] = ()
        if (known_candidate is None and request.instrument_context is None
                and _SOURCE_ACTION_RE.search(request.utterance)):
            identity_candidates = await self._generator.generate(request)
            known_candidate = identity_candidates[0] if identity_candidates else None
        if known_candidate is not None and _candidate_instrument_is_grounded(
            known_candidate, request.utterance,
        ):
            routed_request = replace(request, instrument_context=known_candidate.instrument_symbol)
            viewpoint_grounding = known_candidate.grounding_evidence
        if routed_request.instrument_context is None:
            mention = _idea_guidance_instrument_mention(request.utterance)
            if mention is not None:
                try:
                    resolved_symbol = normalize_a_share_instrument(mention.name).value
                except AshareInstrumentCodeError:
                    if self._instrument_name_resolver is None:
                        resolved_symbol = None
                    else:
                        try:
                            provider_symbol = await asyncio.to_thread(
                                self._instrument_name_resolver,
                                mention.name,
                            )
                            resolved_symbol = normalize_a_share_instrument(provider_symbol).value
                        except (AshareInstrumentCodeError, LookupError, OSError, TimeoutError):
                            # A theme such as “国产算力” is not necessarily a
                            # listed company. Keep the idea unbound rather than
                            # pretending it is a security or blocking analysis.
                            resolved_symbol = None
                if resolved_symbol is not None:
                    name = mention.name
                    routed_request = replace(request, instrument_context=resolved_symbol)
                    viewpoint_grounding = (
                        CandidateGroundingEvidence(
                            path="/instrument/symbol",
                            start=mention.start,
                            end=mention.end,
                            text=name,
                        ),
                    )
        try:
            fixed_entry = _candidate_entry(known_candidate) if known_candidate else None
            fixed_exit = _candidate_exit(known_candidate) if known_candidate else None
        except ValueError:
            return self._idea_guidance_unavailable(
                failure=IdeaGenerationError("execution"), candidate_grounding=viewpoint_grounding,
            )
        try:
            idea_route = await self._idea_router.route(routed_request)
        except IdeaGenerationError as exc:
            return self._idea_guidance_unavailable(
                failure=exc, candidate_grounding=viewpoint_grounding,
            )
        except IdeaResearchUnavailableError:
            return CompileOutcome(
                status=CompileStatus.NEEDS_CLARIFICATION,
                clarification=(
                    "联网事实检索暂时不可用，或没有返回可核验来源。请稍后重试；"
                    "在拿到来源前，系统不会生成时事或情绪驱动的投资策略。"
                ),
                diagnostic_code="idea_research_unavailable",
                candidate_grounding=viewpoint_grounding,
            )
        if idea_route is None:
            return None
        if not 2 <= len(idea_route.proposals) <= 3:
            return self._idea_guidance_unavailable(
                failure=IdeaGenerationError("execution"), candidate_grounding=viewpoint_grounding,
            )

        # Keep user-owned costs outside the proposed buy/sell rules. A known
        # candidate was grounded against the same input and remains authoritative.
        execution_settings = idea_route.execution_settings.merged(
            known_candidate.execution_settings if known_candidate else ExecutionSettingsPatch(),
        )
        instrument_suggestion_declined = idea_route.instrument_suggestion_declined or bool(
            known_candidate and known_candidate.instrument_suggestion_declined
        )

        if routed_request.instrument_context is None and all(
            proposal.instrument_symbol is None and proposal.strategy is None
            for proposal in idea_route.proposals
        ):
            return CompileOutcome(
                status=CompileStatus.NEEDS_CLARIFICATION,
                clarification=(
                    "想试哪个方向？也可以告诉我你想用哪只股票。"
                ),
                diagnostic_code="idea_guidance_required",
                idea_route=idea_route,
                candidate_grounding=viewpoint_grounding,
                execution_settings=execution_settings,
                instrument_suggestion_declined=instrument_suggestion_declined,
            )

        if any(proposal.strategy is not None for proposal in idea_route.proposals):
            emit_progress(
                "strategy_validation",
                "正在校验模型生成的策略结构、标的边界与可回测能力。",
            )
        validated: list[_ValidatedIdeaProposal] = []
        seen_hashes: set[str] = set()
        for proposal in idea_route.proposals:
            route_symbol = routed_request.instrument_context
            if proposal.instrument_symbol is not None and route_symbol is not None:
                try:
                    if (
                        normalize_a_share_instrument(proposal.instrument_symbol).value
                        != normalize_a_share_instrument(route_symbol).value
                    ):
                        continue
                except AshareInstrumentCodeError:
                    continue
            proposal_request = CompileInput(
                utterance=proposal.suggested_utterance,
                instrument_context=proposal.instrument_symbol or route_symbol,
                as_of_date=routed_request.as_of_date,
            )
            current = await self._compile_guidance_proposal(
                proposal_request,
                proposal,
            )
            if current is None:
                _LOGGER.warning(
                    "idea_gate_rejected reason=proposal_compile_failed proposal_index=%d",
                    len(validated) + 1,
                )
                continue
            if ((fixed_entry is not None and current.strategy.entry != fixed_entry)
                    or (fixed_exit is not None and current.strategy.exit != fixed_exit)):
                _LOGGER.warning("idea_gate_rejected reason=changed_explicit_rule")
                continue
            if current.strategy_hash in seen_hashes:
                _LOGGER.warning("idea_gate_rejected reason=duplicate_strategy_hash")
                continue
            seen_hashes.add(current.strategy_hash)
            validated.append(current)
        if len(validated) < 2:
            _LOGGER.warning(
                "idea_gate_rejected reason=insufficient_valid_proposals valid_count=%d",
                len(validated),
            )
            return self._idea_guidance_unavailable(
                failure=IdeaGenerationError("execution"), candidate_grounding=viewpoint_grounding,
            )

        validated = validated[:3]
        if all(item.proposal.strategy is not None for item in validated):
            emit_progress(
                "strategy_candidates_ready",
                f"已有 {len(validated)} 条策略通过 Catalog 与回测边界校验。",
            )
        validated_route = replace(
            idea_route,
            proposals=tuple(item.proposal for item in validated),
        )
        suggestion = (
            validated[0]
            if _looks_like_inferable_trading_intuition(request.utterance)
            and validated[0].proposal.instrument_symbol is not None
            else None
        )
        return CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION,
            clarification=validated_route.understanding,
            diagnostic_code="idea_guidance_required",
            idea_route=validated_route,
            candidate_grounding=viewpoint_grounding,
            suggested_strategy=(None if suggestion is None else suggestion.strategy),
            suggested_strategy_hash=(None if suggestion is None else suggestion.strategy_hash),
            suggested_strategy_choice_id=(
                None if suggestion is None else suggestion.proposal.id
            ),
            suggested_strategy_note=(
                None
                if suggestion is None
                else "我把你说的口语表达暂时理解成这条规则；请确认后再回测。"
            ),
            execution_settings=execution_settings,
            instrument_suggestion_declined=instrument_suggestion_declined,
        )

    @staticmethod
    def _idea_guidance_unavailable(
        *,
        failure: IdeaGenerationError | None = None,
        candidate_grounding: tuple[CandidateGroundingEvidence, ...] = (),
        candidate_provenance: CandidateProvenance | None = None,
    ) -> CompileOutcome:
        message = "本次还没有生成可用的策略方向，请原样重试。"
        diagnostic = "idea_guidance_model_unavailable"
        if failure is not None:
            if failure.stage == "transport":
                message = (
                    "策略生成模型连接超时，请稍后重试。"
                    if failure.timed_out else "策略生成模型连接暂时失败，请稍后重试。"
                )
            elif failure.stage == "schema":
                message = "模型已返回内容，但格式修正后仍不完整；尚未生成可用策略，请重试。"
                diagnostic = "idea_guidance_schema_invalid"
            else:
                message = "模型已返回策略，但买卖条件或回测设置未通过执行校验；本次没有启动回测。"
                diagnostic = "idea_guidance_execution_invalid"
        return CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION,
            clarification=message,
            diagnostic_code=diagnostic,
            candidate_grounding=candidate_grounding,
            candidate_provenance=candidate_provenance,
        )

    async def _compile_guidance_proposal(
        self,
        request: CompileInput,
        proposal: IdeaProposal,
    ) -> _ValidatedIdeaProposal | None:
        """Compile exactly one provider sentence without re-entering idea routing."""

        if proposal.strategy is not None:
            return self._validate_direct_idea_strategy(request, proposal)
        if request.instrument_context is None:
            _LOGGER.warning("idea_gate_rejected reason=proposal_instrument_missing")
            return None
        semantic_error = _unsupported_source_semantics(request.utterance)
        if semantic_error is not None:
            _LOGGER.warning(
                "idea_gate_rejected reason=proposal_source_semantics code=%s", semantic_error
            )
            return None
        try:
            expected_symbol = normalize_a_share_instrument(request.instrument_context).value
        except AshareInstrumentCodeError:
            return None

        valid: dict[str, tuple[CandidateAst, StrategySpec]] = {}
        candidates = await self._generator.generate(request)
        for candidate in candidates[:3]:
            if candidate.unsupported_code is not None or candidate.instrument_symbol is None:
                continue
            try:
                instrument_symbol = normalize_a_share_instrument(candidate.instrument_symbol).value
            except AshareInstrumentCodeError:
                continue
            if instrument_symbol != expected_symbol:
                continue
            if _period_error(candidate, request.as_of_date) is not None:
                continue
            try:
                strategy = self._build_strategy(
                    candidate,
                    request.as_of_date,
                    instrument_symbol=instrument_symbol,
                )
                validate_strategy_against_catalog(strategy, self._catalog)
            except (_UnsupportedCandidateSemantics, ValueError, StrategyCatalogError):
                continue
            capability_ids = _candidate_capability_ids(candidate)
            if not capability_ids:
                continue
            valid[canonical_hash(strategy)] = (candidate, strategy)
        if len(valid) != 1:
            return None

        strategy_hash, (candidate, strategy) = next(iter(valid.items()))
        return _ValidatedIdeaProposal(
            proposal=replace(
                proposal,
                capability_ids=_candidate_capability_ids(candidate),
                instrument_symbol=strategy.instrument.symbol,
            ),
            strategy=strategy,
            strategy_hash=strategy_hash,
        )

    def _validate_direct_idea_strategy(
        self,
        request: CompileInput,
        proposal: IdeaProposal,
    ) -> _ValidatedIdeaProposal | None:
        """Validate provider DSL without interpreting its display sentence."""

        strategy = proposal.strategy
        if strategy is None or request.instrument_context is None:
            return None
        try:
            expected_symbol = normalize_a_share_instrument(request.instrument_context).value
            proposal_symbol = normalize_a_share_instrument(
                proposal.instrument_symbol or ""
            ).value
        except AshareInstrumentCodeError:
            return None
        if (
            strategy.instrument.symbol != expected_symbol
            or proposal_symbol != expected_symbol
            or strategy.catalog.catalog_id != self._catalog_id
            or strategy.catalog.release_version != self._release_version
            or strategy.execution != DailyExecutionPolicy()
            or strategy.backtest.end > request.as_of_date
            or strategy_requires_events(strategy)
            or strategy_requires_financials(strategy)
        ):
            return None
        try:
            validate_strategy_against_catalog(strategy, self._catalog)
        except StrategyCatalogError as exc:
            _LOGGER.warning("idea_gate_rejected reason=catalog_invalid issues=%s", str(exc))
            return None
        except ValueError:
            return None
        strategy_hash = canonical_hash(strategy)
        if proposal.strategy_hash not in {None, strategy_hash}:
            return None
        capability_ids = _strategy_capability_ids(strategy)
        if not capability_ids:
            return None
        return _ValidatedIdeaProposal(
            proposal=replace(
                proposal,
                capability_ids=capability_ids,
                instrument_symbol=expected_symbol,
                strategy=strategy,
                strategy_hash=strategy_hash,
            ),
            strategy=strategy,
            strategy_hash=strategy_hash,
        )

    async def _compile_local_clarification(
        self,
        request: CompileInput,
        diagnostic_code: str,
        *,
        candidate_grounding: tuple[CandidateGroundingEvidence, ...] = (),
        candidate_provenance: CandidateProvenance | None = None,
    ) -> CompileOutcome | None:
        guidance = build_clarification_guidance(request, diagnostic_code)
        if guidance is None:
            return None
        validated_proposals_list: list[IdeaProposal] = []
        for proposal in guidance.route.proposals:
            if await self._clarification_proposal_is_valid(
                request,
                proposal.suggested_utterance,
            ):
                validated_proposals_list.append(proposal)
        validated_proposals = tuple(validated_proposals_list)
        if len(validated_proposals) < 2:
            return None
        route = replace(guidance.route, proposals=validated_proposals[:3])
        return CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION,
            clarification=guidance.question,
            diagnostic_code=diagnostic_code,
            candidate_provenance=candidate_provenance,
            candidate_grounding=_merge_candidate_grounding(
                candidate_grounding,
                guidance.grounding,
            ),
            idea_route=route,
        )

    async def _clarification_proposal_is_valid(
        self,
        request: CompileInput,
        utterance: str,
    ) -> bool:
        """Recompile a server-owned sentence without entering guidance again."""

        if _unsupported_source_semantics(utterance) is not None:
            return False
        proposal_request = CompileInput(
            utterance=utterance,
            instrument_context=request.instrument_context,
            as_of_date=request.as_of_date,
        )
        candidates = await self._generator.generate(proposal_request)
        valid_hashes: set[str] = set()
        for candidate in candidates[:3]:
            if candidate.unsupported_code is not None or candidate.instrument_symbol is None:
                continue
            try:
                instrument_symbol = normalize_a_share_instrument(candidate.instrument_symbol).value
            except AshareInstrumentCodeError:
                continue
            if _period_error(candidate, request.as_of_date) is not None:
                continue
            try:
                strategy = self._build_strategy(
                    candidate,
                    request.as_of_date,
                    instrument_symbol=instrument_symbol,
                )
                validate_strategy_against_catalog(strategy, self._catalog)
            except (_UnsupportedCandidateSemantics, ValueError, StrategyCatalogError):
                continue
            valid_hashes.add(canonical_hash(strategy))
        return len(valid_hashes) == 1

    def _build_strategy(
        self,
        candidate: CandidateAst,
        as_of_date: date,
        *,
        instrument_symbol: str,
    ) -> StrategySpec:
        assert candidate.instrument_symbol is not None
        return self._build_strategy_template(candidate, as_of_date).bind(instrument_symbol)

    def _unbound_candidate_proposal(
        self, request: CompileInput, candidate: CandidateAst,
    ) -> IdeaProposal | None:
        """Retain model-parsed complete rules while only the stock is missing."""
        if (not candidate.entry or not candidate.exit
                or _period_error(candidate, request.as_of_date) is not None):
            return None
        summaries: list[str] = []
        for leg in ("entry", "exit"):
            spans = [item for item in candidate.grounding_evidence
                     if item.path.startswith(f"/{leg}/") or item.path == f"/{leg}"]
            if not spans:
                return None
            summaries.append(request.utterance[
                min(item.start for item in spans):max(item.end for item in spans)
            ][:160])
        try:
            template = self._build_strategy_template(candidate, request.as_of_date)
        except (ValueError, _UnsupportedCandidateSemantics):
            return None
        return IdeaProposal(
            id=f"idea_{canonical_hash(template).removeprefix('sha256:')[:12]}",
            title="按当前买卖规则", hypothesis="保留用户已给出的完整规则，仅选择回测股票。",
            entry_summary=summaries[0], exit_summary=summaries[1],
            suggested_utterance=f"买入：{summaries[0]}；卖出：{summaries[1]}。",
            capability_ids=(), assumptions=(), confidence=candidate.confidence,
            strategy_template=template,
        )

    def _build_strategy_template(
        self, candidate: CandidateAst, as_of_date: date,
    ) -> UnboundIdeaStrategy:
        entry, exit_rule = _candidate_entry(candidate), _candidate_exit(candidate)
        assert entry is not None and exit_rule is not None
        start, end = _resolve_backtest_period(
            candidate,
            as_of_date=as_of_date,
            default_lookback_years=self._lookback_years,
        )
        has_events = any(
            isinstance(item, EventIntent) for item in (*candidate.entry, *candidate.exit)
        )
        has_financials = any(
            isinstance(item, FinancialIntent) for item in (*candidate.entry, *candidate.exit)
        )
        execution = (
            DailyExecutionPolicy(
                data_capability="daily_ohlcv_events_financials",
                evaluation_frequency="event_financial_available_plus_1d_close",
            )
            if has_events and has_financials
            else DailyExecutionPolicy(
                data_capability="daily_ohlcv_events",
                evaluation_frequency="event_available_plus_1d_close",
            )
            if has_events
            else DailyExecutionPolicy(
                data_capability="daily_ohlcv_financials",
                evaluation_frequency="financial_available_plus_1d_close",
            )
            if has_financials
            else DailyExecutionPolicy()
        )
        return UnboundIdeaStrategy(
            catalog=CatalogRef(
                catalog_id=self._catalog_id,
                release_version=self._release_version,
            ),
            entry=entry,
            exit=exit_rule,
            execution=execution,
            backtest=BacktestConfig(
                start=start,
                end=end,
                initial_cash_cny=(
                    candidate.initial_cash_cny
                    if candidate.initial_cash_cny is not None
                    else self._initial_cash_cny
                ),
            ),
        )


def _candidate_entry(candidate: CandidateAst) -> Condition | None:
    conditions = tuple(_to_condition(item) for item in candidate.entry)
    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return (AllCondition(children=conditions) if candidate.entry_join == "all"
            else AnyCondition(children=conditions))


def _candidate_exit(candidate: CandidateAst) -> FirstOfExit | None:
    rules = tuple(_to_exit_rule(item) for item in candidate.exit)
    if not rules:
        return None
    if candidate.exit_join == "all" and len(rules) > 1:
        conditions = tuple(item for item in rules if not isinstance(
            item, (HoldingPeriodExit, PositionReturnExit, TrailingDrawdownExit),
        ))
        if len(conditions) != len(rules):
            raise _UnsupportedCandidateSemantics(POSITION_AWARE_EXIT_AND_UNSUPPORTED)
        return FirstOfExit(children=(AllCondition(children=conditions),))
    return FirstOfExit(children=rules)


def _looks_like_broad_viewpoint(utterance: str) -> bool:
    """Identify a pure viewpoint before asking the strict strategy translator.

    A request to backtest is not itself a trading rule. Actual entry/exit or
    indicator syntax still goes to translation first; metadata alone must not
    prevent the model from turning a viewpoint into strategy choices.
    """

    normalized = re.sub(r"\s+", "", utterance).casefold()
    return bool(normalized) and _SOURCE_ACTION_RE.search(normalized) is None and not any(
        marker in normalized for marker in _STRATEGY_SYNTAX_MARKERS if marker != "回测"
    )


def has_explicit_idea_instrument_reference(utterance: str) -> bool:
    """Return whether an idea prompt itself names a candidate instrument.

    This is a lexical routing check, not an identity decision.  A company name
    still has to pass the injected server-owned resolver inside the compiler;
    the check only prevents an old remembered symbol from taking precedence
    over a target the user just wrote explicitly.
    """

    return _idea_guidance_instrument_mention(utterance) is not None


def _original_instrument_reference(reference: str, utterance: str) -> str | None:
    """Ground an equivalent model-normalised code back to its literal input span."""
    try:
        expected = normalize_a_share_instrument(reference).value
    except AshareInstrumentCodeError:
        return reference if reference in utterance else None
    for match in re.finditer(
        r"(?<![A-Za-z0-9])\d{6}(?:\.(?:SH|SZ|BJ))?(?![A-Za-z0-9])", utterance, re.I,
    ):
        try:
            if normalize_a_share_instrument(match.group()).value == expected:
                return match.group()
        except AshareInstrumentCodeError:
            continue
    return None


def _idea_guidance_instrument_mention(utterance: str) -> _InstrumentMention | None:
    """Recover a stock-first idea prompt before the strict translator runs.

    We keep this intentionally narrow: only obviously guidance-seeking,
    non-executable prompts may bind a leading company name here.
    """

    if _NEGATED_VIEWPOINT_INSTRUMENT_RE.search(utterance):
        return None
    explicit_code = _IDEA_GUIDANCE_SECURITY_CODE_RE.search(utterance)
    if explicit_code is not None:
        return _InstrumentMention(
            name=explicit_code.group(0),
            start=explicit_code.start(),
            end=explicit_code.end(),
        )
    analyzed_instrument = _ANALYZE_INSTRUMENT_RE.search(utterance)
    if analyzed_instrument is not None:
        return _InstrumentMention(
            name=analyzed_instrument.group("name"),
            start=analyzed_instrument.start("name"),
            end=analyzed_instrument.end("name"),
        )
    explicit_viewpoint = _VIEWPOINT_INSTRUMENT_RE.search(utterance)
    if explicit_viewpoint is not None:
        return _InstrumentMention(
            name=explicit_viewpoint.group("name"),
            start=explicit_viewpoint.start("name"),
            end=explicit_viewpoint.end("name"),
        )
    stripped = _LEADING_GREETING_RE.sub("", utterance).strip()
    if not stripped:
        return None
    cue = _STOCK_FIRST_IDEA_GUIDANCE_CUE_RE.search(stripped)
    if cue is None:
        return None
    candidate = stripped[: cue.start()].strip(" ，,。；;!！?？")
    candidate = re.sub(r"(?:的|这只|这个)$", "", candidate).strip(" ，,。；;!！?？")
    if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9*STst·\-]{2,32}", candidate):
        return None
    offset = utterance.index(stripped)
    start = offset + stripped.index(candidate)
    end = start + len(candidate)
    return _InstrumentMention(
        name=candidate,
        start=start,
        end=end,
    )


def _candidate_capability_ids(candidate: CandidateAst) -> tuple[str, ...]:
    """Derive public capability labels from the compiler-validated AST."""

    capability_ids: list[str] = []
    for intent in (*candidate.entry, *candidate.exit):
        capability_id: str
        if isinstance(intent, IndicatorIntent):
            capability_id = intent.indicator_id
        elif isinstance(intent, EventIntent):
            capability_id = intent.event_code
        elif isinstance(intent, FinancialIntent):
            capability_id = intent.metric_id.value
        elif isinstance(intent, HoldingPeriodIntent):
            capability_id = "strategy.holding_period"
        elif isinstance(intent, PositionReturnIntent):
            capability_id = f"strategy.{intent.trigger}"
        else:
            capability_id = "strategy.trailing_drawdown"
        if capability_id not in capability_ids:
            capability_ids.append(capability_id)
    return tuple(capability_ids)


def _strategy_capability_ids(strategy: StrategySpec) -> tuple[str, ...]:
    capability_ids: list[str] = []
    for condition in iter_indicator_conditions(strategy):
        if condition.indicator_id not in capability_ids:
            capability_ids.append(condition.indicator_id)
    if next(iter_holding_period_exits(strategy), None) is not None:
        capability_ids.append("strategy.holding_period")
    for item in iter_position_return_exits(strategy):
        capability_id = f"strategy.{item.trigger}"
        if capability_id not in capability_ids:
            capability_ids.append(capability_id)
    if next(iter_trailing_drawdown_exits(strategy), None) is not None:
        capability_ids.append("strategy.trailing_drawdown")
    return tuple(capability_ids)


def _to_condition(intent: SignalIntent) -> Condition:
    if isinstance(intent, FinancialIntent):
        return FinancialConditionV1(
            metric_id=intent.metric_id,
            definition_version=intent.definition_version,
            report_type=intent.report_type,
            period_basis=intent.period_basis,
            statement_scope=intent.statement_scope,
            comparator=intent.comparator,
            value=intent.value,
            unit=intent.unit,
        )
    if isinstance(intent, EventIntent):
        return EventCondition(
            event_code=intent.event_code,
            definition_version=intent.definition_version,
            trigger=intent.trigger,
            attributes=intent.attributes_dict(),
            document_text=(
                None
                if intent.document_text is None
                else EventDocumentTextPredicate(
                    term=intent.document_text.term,
                    match_mode=intent.document_text.match_mode,
                    comparator=intent.document_text.comparator,
                    value=intent.document_text.value,
                    case_sensitive=intent.document_text.case_sensitive,
                )
            ),
        )
    return IndicatorCondition(
        indicator_id=intent.indicator_id,
        definition_version=intent.definition_version,
        params=intent.params_dict(),
        trigger=intent.trigger,
        value=intent.value,
    )


def _to_exit_rule(intent: ExitIntent) -> ExitRule:
    if isinstance(intent, HoldingPeriodIntent):
        return HoldingPeriodExit(sessions=intent.sessions)
    if isinstance(intent, PositionReturnIntent):
        return PositionReturnExit(
            trigger=intent.trigger,
            threshold_pct=intent.threshold_pct,
        )
    if isinstance(intent, TrailingDrawdownIntent):
        return TrailingDrawdownExit(threshold_pct=intent.threshold_pct)
    return _to_condition(intent)


def _subtract_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _period_error(candidate: CandidateAst, as_of_date: date) -> str | None:
    has_start = candidate.backtest_start is not None
    has_end = candidate.backtest_end is not None
    if has_start != has_end:
        return "backtest_date_range_incomplete"
    if has_start and candidate.backtest_lookback_years is not None:
        return "backtest_date_range_ambiguous"
    if (
        candidate.backtest_start is not None
        and candidate.backtest_end is not None
        and candidate.backtest_start > candidate.backtest_end
    ):
        return "backtest_date_range_reversed"
    if candidate.backtest_lookback_years is not None and not (
        1 <= candidate.backtest_lookback_years <= 100
    ):
        return "backtest_lookback_invalid"
    if candidate.backtest_end is not None and candidate.backtest_end > as_of_date:
        return "backtest_end_after_as_of_date"
    return None


def _merge_candidate_grounding(
    *groups: tuple[CandidateGroundingEvidence, ...],
) -> tuple[CandidateGroundingEvidence, ...]:
    """Preserve source grounding while adding server-owned clarification spans."""

    merged: list[CandidateGroundingEvidence] = []
    seen: set[tuple[str, int, int, str]] = set()
    for group in groups:
        for item in group:
            identity = (item.path, item.start, item.end, item.text)
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(item)
    return tuple(merged)


def _unsupported_source_semantics(utterance: str) -> str | None:
    """Reject source meaning that the bounded daily DSL cannot preserve.

    This guard intentionally runs before every candidate generator.  It keeps
    deterministic parsing and bounded-provider translation under one rule: a
    valid-looking daily candidate cannot silently erase an explicit timeframe,
    execution instruction, report filter, or compound MACD qualifier from the
    user's sentence.
    """

    text = unicodedata.normalize("NFKC", utterance).casefold()
    if _PREVIOUS_SESSION_LIMIT_UP_ENTRY_RE.search(text) is not None:
        return "previous_session_limit_up_capability_unavailable"
    if _NON_DAILY_TIMEFRAME_RE.search(text) is not None:
        return "non_daily_timeframe_not_supported"
    execution_text = _DAILY_RETURN_ACTION_RE.sub("", text)
    # Here "当日" describes a volume/amount observation, not the order time.
    # Strip only that prefix for this gate; keep the original model input and
    # any separate "立即/当天买入" instruction in the rest of the clause intact.
    execution_text = _DAILY_VOLUME_OBSERVATION_PREFIX_RE.sub("", execution_text)
    if _SAME_SESSION_EXECUTION_RE.search(execution_text) is not None:
        return "same_session_execution_not_supported"
    if _NON_DEFAULT_EXECUTION_RE.search(text) is not None:
        return "execution_price_time_not_supported"
    if _has_unmodeled_delayed_execution(text):
        return "execution_price_time_not_supported"
    if _SPECIFIC_REPORT_PERIOD_RE.search(text) is not None:
        return "event_report_period_filter_not_supported"
    for clause in re.split(r"[,，。；;！!?？]", text):
        if (
            _PERIODIC_REPORT_RE.search(clause) is not None
            and _UNMODELED_REPORT_FILTER_RE.search(clause) is not None
        ):
            return "event_attribute_filter_not_supported"
    if _MACD_UNMODELED_QUALIFIER_RE.search(text) is not None:
        return "technical_qualifier_not_supported"
    if _has_unjoined_entry_actions(text):
        return "ambiguous_boolean_expression"
    if re.search(r"(?:obv|能量潮)[^,，。；;]{0,8}变化[^,，。；;]{0,8}(?:买入|卖出|买|卖)", text):
        return "ambiguous_obv_direction"
    if re.search(
        r"(?:成交量|量能|量比)[^,，。；;]{0,8}变化[^,，。；;]{0,8}(?:买入|卖出|买|卖)",
        text,
    ):
        return "ambiguous_volume_direction"
    if _has_named_indicator_without_trigger(text):
        return "indicator_trigger_requires_clarification"
    return None


def _has_unjoined_entry_actions(text: str) -> bool:
    """Do not silently convert repeated entry clauses into an OR strategy."""

    entries = tuple(
        match
        for match in re.finditer(r"(?<![购超])(?:买入|买进|买)", text)
        if not text[match.end() :].startswith(("后", "之后", "以后"))
    )
    return any(
        re.search(r"(?:并且|而且|同时|以及|且|或者|或是|任一|或)", text[left.end() : right.start()])
        is None
        for left, right in pairwise(entries)
    )


def _has_unmodeled_delayed_execution(text: str) -> bool:
    """Keep signal-delayed orders distinct from supported fill-anchored exits."""

    for clause in re.split(r"[,，。；;!！?？]", text):
        matches = tuple(_EXPLICIT_DELAYED_EXECUTION_RE.finditer(clause))
        if not matches:
            continue
        supported_spans = [match.span() for match in _SUPPORTED_HOLDING_EXIT_RE.finditer(clause)]
        bare_holding = _SUPPORTED_BARE_HOLDING_EXIT_RE.fullmatch(clause)
        if bare_holding is not None:
            supported_spans.append(bare_holding.span())
        for match in matches:
            if match.group("action") in {"买入", "买进", "建仓", "下单", "成交"}:
                return True
            if not any(
                start <= match.start() and match.end() <= end for start, end in supported_spans
            ):
                return True
    return False


def _has_named_indicator_without_trigger(text: str) -> bool:
    """Detect a named indicator plus action whose trigger was never stated.

    This is intentionally a source-level guard rather than a parser default.
    It therefore applies equally to the deterministic fast path and bounded
    provider candidates.  Clauses with partial trigger language continue to the
    normal parser/catalog validation, which can return a more specific error.
    """

    prefixed_markers = tuple(re.finditer(r"(买入|卖出)条件(?:是|为|：|:)?", text, re.IGNORECASE))
    if prefixed_markers:
        clauses = tuple(
            text[
                marker.end() : (
                    prefixed_markers[index + 1].start()
                    if index + 1 < len(prefixed_markers)
                    else len(text)
                )
            ].strip(" ,，。；;、")
            for index, marker in enumerate(prefixed_markers)
        )
    else:
        clauses_list: list[str] = []
        cursor = 0
        for marker in _SOURCE_ACTION_RE.finditer(text):
            clause = re.sub(
                r"^[ ,，。；;、]*(?:然后|再|则|就)?",
                "",
                text[cursor : marker.start()],
            ).strip(" ,，。；;、")
            cursor = marker.end()
            if clause:
                clauses_list.append(clause)
        clauses = tuple(clauses_list)

    return any(
        _NAMED_INDICATOR_RE.search(clause) is not None
        and _EXPLICIT_INDICATOR_TRIGGER_RE.search(clause) is None
        for clause in clauses
    )


def _resolve_backtest_period(
    candidate: CandidateAst,
    *,
    as_of_date: date,
    default_lookback_years: int,
) -> tuple[date, date]:
    if candidate.backtest_start is not None and candidate.backtest_end is not None:
        return candidate.backtest_start, candidate.backtest_end
    lookback_years = candidate.backtest_lookback_years or default_lookback_years
    return _subtract_years(as_of_date, lookback_years), as_of_date


def _period_provenance(
    candidate: CandidateAst,
    *,
    snapshot_anchored: bool = False,
) -> tuple[str, str]:
    if candidate.backtest_start is not None:
        return "utterance/explicit_date_range", "utterance/explicit_date_range"
    end_source = "data_snapshot/end" if snapshot_anchored else "request/as_of_date"
    if candidate.backtest_lookback_years is not None:
        return "utterance/relative_lookback", end_source
    return "default/lookback_years", end_source


def _selected_clarification_proposal(
    outcome: CompileOutcome,
    answer: str,
) -> IdeaProposal | None:
    if outcome.idea_route is None:
        return None
    normalized = answer.strip()
    proposals = outcome.idea_route.proposals
    for proposal in proposals:
        if normalized in {proposal.id, proposal.title, proposal.suggested_utterance}:
            return proposal
    ordinal_match = re.fullmatch(
        r"(?:我?选|选择|用)?\s*(?:第)?\s*([123一二三])\s*(?:个|项|条)?",
        normalized,
    )
    if ordinal_match is None:
        return None
    ordinal = {"1": 1, "一": 1, "2": 2, "二": 2, "3": 3, "三": 3}[ordinal_match.group(1)]
    if ordinal > len(proposals):
        return None
    return proposals[ordinal - 1]


def _idea_candidate_provenance(
    outcome: CompileOutcome,
    selected: IdeaProposal,
) -> CandidateProvenance | None:
    route = outcome.idea_route
    if route is None or route.provenance is None:
        return None
    try:
        candidate_rank = route.proposals.index(selected) + 1
    except ValueError:
        return None
    provenance = route.provenance
    return CandidateProvenance(
        source="bounded_provider",
        provider=provenance.provider,
        model=provenance.model,
        prompt_version=provenance.prompt_version,
        schema_version=provenance.schema_version,
        capability_projection_version=provenance.capability_projection_version,
        capability_projection_hash=provenance.capability_projection_hash,
        upstream_pattern_commit=provenance.upstream_pattern_commit,
        candidate_rank=candidate_rank,
    )


def _clarification_pragmatic_issue(answer: str) -> str | None:
    normalized = answer.strip()
    if _CLARIFICATION_NEGATION_RE.search(normalized):
        return "negation"
    if _CLARIFICATION_EXAMPLE_RE.search(normalized):
        return "example"
    if _CLARIFICATION_QUESTION_RE.search(normalized):
        return "question"
    normalized_casefold = normalized.casefold()
    conversational_text = _LEADING_GREETING_RE.sub("", normalized).strip() or normalized
    if not any(
        marker in normalized_casefold for marker in _STRATEGY_SYNTAX_MARKERS
    ) and _CLARIFICATION_CONVERSATION_RE.fullmatch(conversational_text):
        return "conversation"
    return None


def _looks_like_initial_conversation(utterance: str) -> bool:
    normalized = utterance.strip()
    if classify_clarification_turn(normalized) is TurnIntent.CASUAL:
        return True
    without_greeting = _LEADING_GREETING_RE.sub("", normalized).strip()
    if without_greeting != normalized and not without_greeting:
        return True
    strategy_text = (without_greeting or normalized).casefold()
    if any(marker in strategy_text for marker in _STRATEGY_SYNTAX_MARKERS):
        return False
    return bool(_INITIAL_CONVERSATION_RE.fullmatch(without_greeting or normalized))


def _looks_like_inferable_trading_intuition(utterance: str) -> bool:
    return _INFERABLE_TRADING_INTUITION_RE.search(utterance) is not None


def _candidate_instrument_is_grounded(candidate: CandidateAst, utterance: str) -> bool:
    """Accept only a code in the source or server-resolver grounding evidence."""

    if candidate.instrument_symbol is None:
        return False
    try:
        symbol = normalize_a_share_instrument(candidate.instrument_symbol).value
    except AshareInstrumentCodeError:
        return False
    if any(item.path == "/instrument/symbol" for item in candidate.grounding_evidence):
        return True
    digits = symbol.split(".", 1)[0]
    return (
        re.search(
            rf"(?<!\d){re.escape(digits)}(?:\.(?:SH|SZ|BJ))?(?!\d)",
            utterance,
            re.I,
        )
        is not None
    )


def _pragmatic_fallback(issue: str | None) -> str:
    if issue == "negation":
        return "明白，这个方向你不想用，我不会把它写进规则。"
    if issue == "question":
        return "你是在询问这个条件是否合适，我先不把它当作决定。"
    if issue == "example":
        return "我把这句理解为举例，不会直接写进规则。"
    if issue == "unsupported":
        return "这项补充目前不能安全执行，我先保留原规则。"
    return "我听到了，不过这句暂时还没有回答刚才缺的那一项；已经说清的部分我会继续保留。"


def _clarification_followup(question: str | None) -> str:
    normalized = (question or "请补充缺失条件。").strip()
    for prefix in ("接下来只差：", "现在只差：", "只差："):
        if normalized.startswith(prefix):
            normalized = normalized.removeprefix(prefix).strip()
            break
    if normalized.startswith(("请", "还需要", "请选择")):
        followup = normalized
    else:
        followup = f"还需要你补充：{normalized}"
    if not followup.endswith(("。", "！", "!", "？", "?")):
        followup += "。"
    return followup


def _textual_suggestion_hint(
    suggestions: tuple[ClarificationSuggestion, ...],
) -> str:
    """Offer server-validated examples as prose, never as an implied UI control."""

    if not suggestions:
        return ""
    examples = "、".join(f"“{item.preview}”" for item in suggestions[:2])
    return f"你可以直接在输入框补充，例如{examples}；也可以按自己的想法描述。"


def _merge_clarification_answer(
    original: str,
    answer: str,
    *,
    diagnostic_code: str,
) -> str:
    supplement = answer.strip(" ，,。；;\n\t")
    base = original.strip(" ，,。；;\n\t")
    if diagnostic_code in _INSTRUMENT_CLARIFICATION_CODES:
        return f"{supplement}，{base}"
    return merge_clarification_supplement(base, supplement)


def _merge_viewpoint_continuation(original: str, answer: str) -> str:
    """Join two explicit viewpoint fragments without inventing strategy text."""

    base = original.strip(" ，,。；;\n\t")
    continuation = answer.strip(" ，,。；;\n\t")
    return f"{base}，{continuation}"


def _has_grounded_instrument(outcome: CompileOutcome) -> bool:
    """Return whether the server resolved an instrument from the new answer."""

    return any(item.path == "/instrument/symbol" for item in outcome.candidate_grounding)


def _clarification_instrument_context(
    original_input: CompileInput,
    prior_outcome: CompileOutcome,
) -> str | None:
    """Reuse only a server-resolved instrument from the stored clarification state."""

    if original_input.instrument_context is not None:
        return original_input.instrument_context
    if prior_outcome.idea_route is None:
        return None
    symbol = prior_outcome.idea_route.asset_mapping.instrument_symbol
    if symbol is None:
        return None
    return normalize_a_share_instrument(symbol).value


def _clarification_suggestions(
    outcome: CompileOutcome,
) -> tuple[ClarificationSuggestion, ...]:
    if outcome.idea_route is None:
        return ()
    return tuple(
        ClarificationSuggestion(
            id=item.id,
            title=item.title,
            # Every exposed sentence was recompiled and passed the active
            # Catalog gate before it reached ``idea_route``.  Returning the
            # complete sentence lets the user answer with a real rule instead
            # of forcing the UI to turn an abstract hypothesis into DSL.
            preview=item.suggested_utterance,
        )
        for item in outcome.idea_route.proposals[:3]
    )


def _rank_clarification_suggestions(
    suggestions: tuple[ClarificationSuggestion, ...],
    recommended_ids: tuple[str, ...],
) -> tuple[ClarificationSuggestion, ...]:
    if not recommended_ids:
        return suggestions
    by_id = {item.id: item for item in suggestions}
    ranked = [by_id[item] for item in recommended_ids if item in by_id]
    ranked.extend(item for item in suggestions if item.id not in recommended_ids)
    return tuple(ranked)


def _shanghai_today() -> date:
    return datetime.now(SHANGHAI).date()
