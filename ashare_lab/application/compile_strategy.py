"""Use case for turning one sentence into a validated Strategy DSL revision."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from itertools import pairwise
from typing import Literal
from zoneinfo import ZoneInfo

from ashare_lab.application.clarification_guidance import build_clarification_guidance
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
    PositionReturnIntent,
    SignalIntent,
    TrailingDrawdownIntent,
)
from ashare_lab.ports.clarification_dialogue import (
    ClarificationDialogueRequest,
    ClarificationDialogueRouter,
    ClarificationOption,
)
from ashare_lab.ports.idea_routing import IdeaProposal, IdeaRoute, IdeaRouter

DEFAULT_INITIAL_CASH_CNY = 1_000_000
SHANGHAI = ZoneInfo("Asia/Shanghai")
POSITION_AWARE_EXIT_AND_UNSUPPORTED = "position_aware_exit_and_not_supported"
_IDEA_ROUTE_DIAGNOSTIC_CODES = frozenset(
    {"no_supported_signal_recognized", "candidate_provider_invalid_output"}
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
    r"(?:买入|卖出|买进|卖掉|下单|成交|买|卖)|"
    r"(?:马上|立刻|立即|即时)[^,，。；;]{0,6}"
    r"(?:买入|卖出|买进|卖掉|下单|成交|买|卖)|"
    r"(?:买入|卖出|买进|卖掉|下单|成交|买|卖)"
    r"[^,，。；;]{0,4}(?:马上|立刻|立即|即时))"
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

    async def answer_clarification(
        self,
        *,
        original_input: CompileInput,
        prior_outcome: CompileOutcome,
        answer: str,
    ) -> ClarificationTurnOutcome:
        """Apply one answer without granting the model any strategy authority."""

        if (
            prior_outcome.status is not CompileStatus.NEEDS_CLARIFICATION
            or prior_outcome.diagnostic_code is None
        ):
            raise ValueError("draft revision is not awaiting clarification")
        selected_utterance = _selected_clarification_utterance(prior_outcome, answer)
        pragmatic_issue = (
            None if selected_utterance is not None else _clarification_pragmatic_issue(answer)
        )
        if pragmatic_issue is None:
            merged_input = CompileInput(
                utterance=(
                    selected_utterance
                    if selected_utterance is not None
                    else _merge_clarification_answer(
                        original_input.utterance,
                        answer,
                        diagnostic_code=prior_outcome.diagnostic_code,
                    )
                ),
                instrument_context=original_input.instrument_context,
                as_of_date=original_input.as_of_date,
            )
            recompiled = await self.compile(merged_input)
        else:
            merged_input = original_input
            recompiled = prior_outcome
        progressed = recompiled.status is CompileStatus.READY or (
            recompiled.status is CompileStatus.NEEDS_CLARIFICATION
            and recompiled.diagnostic_code != prior_outcome.diagnostic_code
        )
        if progressed:
            if recompiled.status is CompileStatus.READY:
                message = "好，我已经把这句补充接到刚才的规则里，买入和卖出条件都完整了。"
            else:
                next_question = recompiled.clarification or "还需要再补充一项信息。"
                message = f"明白，这部分已经接上了。接下来只差：{next_question}"
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
        assessment = None
        context_summary = (
            prior_outcome.idea_route.understanding
            if prior_outcome.idea_route is not None
            else "你刚才已经说清的部分我会原样保留。"
        ).rstrip("。！？!? ")
        if self._clarification_dialogue_router is not None:
            assessment = await self._clarification_dialogue_router.assess(
                ClarificationDialogueRequest(
                    answer=answer.strip(),
                    diagnostic_code=prior_outcome.diagnostic_code,
                    question=prior_outcome.clarification or "请补充缺失的策略条件。",
                    context_summary=context_summary,
                    options=tuple(
                        ClarificationOption(
                            id=item.id,
                            title=item.title,
                            preview=item.preview,
                        )
                        for item in suggestions
                    ),
                )
            )
        if assessment is not None:
            suggestions = _rank_clarification_suggestions(
                suggestions,
                assessment.recommended_option_ids,
            )
            acknowledgement = _provider_acknowledgement(assessment.acknowledgement_id)
            message = (
                f"{acknowledgement}。{context_summary}。"
                f"接下来只差：{prior_outcome.clarification or '请补充缺失条件。'}"
                "你可以从下面选，也可以直接打字告诉我。"
            )
        else:
            acknowledgement = _pragmatic_fallback(pragmatic_issue)
            message = (
                f"{acknowledgement}{context_summary}。"
                f"接下来只差："
                f"{prior_outcome.clarification or '请补充缺失条件。'}"
                "你可以从下面选，也可以直接打字告诉我。"
            )
        return ClarificationTurnOutcome(
            reply_kind="clarification",
            assistant_message=message,
            outcome=prior_outcome,
            compile_input=original_input,
            revision_changed=False,
            suggestions=suggestions,
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
            else CompileInput(
                utterance=request.utterance,
                instrument_context=request.instrument_context,
                as_of_date=effective_as_of_date,
            )
        )
        source_semantic_diagnostic = _unsupported_source_semantics(request.utterance)
        if source_semantic_diagnostic is not None:
            if source_semantic_diagnostic in _LOCAL_CLARIFICATION_ROUTE_CODES:
                clarification_outcome = await self._compile_local_clarification(
                    effective_request,
                    source_semantic_diagnostic,
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
        if self._idea_router is not None and _looks_like_broad_viewpoint(
            effective_request.utterance
        ):
            idea_outcome = await self._compile_idea_guidance(effective_request)
            if idea_outcome is not None:
                return idea_outcome
        candidates = await self._generator.generate(effective_request)
        if not candidates:
            return CompileOutcome(
                status=CompileStatus.UNSUPPORTED,
                diagnostic_code="no_candidate_generated",
            )
        if self._idea_router is not None and all(
            item.unsupported_code in _IDEA_ROUTE_DIAGNOSTIC_CODES for item in candidates
        ):
            idea_outcome = await self._compile_idea_guidance(effective_request)
            if idea_outcome is not None:
                return idea_outcome
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
                clarification_outcome = await self._compile_local_clarification(
                    clarification_request,
                    candidate.unsupported_code,
                    candidate_grounding=candidate.grounding_evidence,
                    candidate_provenance=candidate.provenance,
                )
                if clarification_outcome is not None:
                    return clarification_outcome
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
                )
            if candidate.unsupported_code == "candidate_provider_low_confidence":
                return CompileOutcome(
                    status=CompileStatus.NEEDS_CLARIFICATION,
                    clarification=(
                        "我不够确定这句话里的买卖条件。请在一句话里同时写清："
                        "什么条件买入、什么条件卖出；系统不会替你猜默认策略。"
                    ),
                    diagnostic_code=candidate.unsupported_code,
                    candidate_provenance=candidate.provenance,
                    candidate_grounding=candidate.grounding_evidence,
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
                    clarification="我只差股票：请确认要回测哪一只 A 股（6 位代码）？",
                    diagnostic_code="instrument_required",
                    candidate_provenance=first_provenance,
                    candidate_grounding=candidates[0].grounding_evidence,
                    candidate_rejections=tuple(rejections),
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
            FieldProvenance(path="/backtest/initial_cash_cny", source="default/initial_cash"),
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
        )

    async def _compile_idea_guidance(
        self,
        request: CompileInput,
    ) -> CompileOutcome | None:
        if self._idea_router is None:
            return None
        if request.instrument_context is None:
            return CompileOutcome(
                status=CompileStatus.NEEDS_CLARIFICATION,
                clarification="我只差股票：请确认要回测哪一只 A 股（6 位代码）？",
                diagnostic_code="instrument_required",
            )
        idea_route = await self._idea_router.route(request)
        if (
            idea_route is None
            or idea_route.asset_mapping.instrument_symbol is None
            or not 2 <= len(idea_route.proposals) <= 3
        ):
            return None
        return CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION,
            clarification=(
                "我理解这是一个观点，还不是可直接执行的交易规则。"
                "请从下面的价格行为代理中选一种，选择后仍会通过现有"
                " DSL 和 Catalog 校验，系统不会自动执行。"
            ),
            diagnostic_code="idea_guidance_required",
            idea_route=idea_route,
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
        entry_conditions = tuple(_to_condition(item) for item in candidate.entry)
        entry: Condition = (
            entry_conditions[0]
            if len(entry_conditions) == 1
            else (
                AllCondition(children=entry_conditions)
                if candidate.entry_join == "all"
                else AnyCondition(children=entry_conditions)
            )
        )
        exit_rules = tuple(_to_exit_rule(item) for item in candidate.exit)
        position_aware_rules = tuple(
            item
            for item in exit_rules
            if isinstance(
                item,
                (HoldingPeriodExit, PositionReturnExit, TrailingDrawdownExit),
            )
        )
        if candidate.exit_join == "all" and len(exit_rules) > 1 and position_aware_rules:
            raise _UnsupportedCandidateSemantics(POSITION_AWARE_EXIT_AND_UNSUPPORTED)
        if candidate.exit_join == "all":
            condition_rules = tuple(
                item
                for item in exit_rules
                if not isinstance(
                    item,
                    (HoldingPeriodExit, PositionReturnExit, TrailingDrawdownExit),
                )
            )
            exit_children: tuple[ExitRule, ...] = (
                (AllCondition(children=condition_rules),)
                if len(condition_rules) > 1
                else exit_rules
            )
        else:
            exit_children = exit_rules
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
        return StrategySpec(
            catalog=CatalogRef(
                catalog_id=self._catalog_id,
                release_version=self._release_version,
            ),
            instrument=Instrument(symbol=instrument_symbol),
            entry=entry,
            exit=FirstOfExit(children=exit_children),
            execution=execution,
            backtest=BacktestConfig(
                start=start,
                end=end,
                initial_cash_cny=self._initial_cash_cny,
            ),
        )


def _looks_like_broad_viewpoint(utterance: str) -> bool:
    """Identify a pure viewpoint before asking the strict strategy translator.

    This is only a routing shortcut.  Any sentence containing recognizable
    strategy syntax still goes through the existing deterministic/provider
    compiler first, so the idea layer cannot steal or weaken a real rule.
    """

    normalized = re.sub(r"\s+", "", utterance).casefold()
    return bool(normalized) and not any(marker in normalized for marker in _STRATEGY_SYNTAX_MARKERS)


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


def _selected_clarification_utterance(
    outcome: CompileOutcome,
    answer: str,
) -> str | None:
    if outcome.idea_route is None:
        return None
    normalized = answer.strip()
    proposals = outcome.idea_route.proposals
    for proposal in proposals:
        if normalized in {proposal.id, proposal.title, proposal.suggested_utterance}:
            return proposal.suggested_utterance
    ordinal_match = re.fullmatch(
        r"(?:我?选|选择|用)?\s*(?:第)?\s*([123一二三])\s*(?:个|项|条)?",
        normalized,
    )
    if ordinal_match is None:
        return None
    ordinal = {"1": 1, "一": 1, "2": 2, "二": 2, "3": 3, "三": 3}[ordinal_match.group(1)]
    if ordinal > len(proposals):
        return None
    return proposals[ordinal - 1].suggested_utterance


def _clarification_pragmatic_issue(answer: str) -> str | None:
    normalized = answer.strip()
    if _CLARIFICATION_NEGATION_RE.search(normalized):
        return "negation"
    if _CLARIFICATION_EXAMPLE_RE.search(normalized):
        return "example"
    if _CLARIFICATION_QUESTION_RE.search(normalized):
        return "question"
    return None


def _pragmatic_fallback(issue: str | None) -> str:
    if issue == "negation":
        return "明白，这个方向你不想用，我不会把它写进规则。"
    if issue == "question":
        return "你是在询问这个条件是否合适，我先不把它当作决定。"
    if issue == "example":
        return "我把这句理解为举例，不会直接写进规则。"
    if issue == "unsupported":
        return "这项补充目前不能安全执行，我先保留原规则。"
    return "我听到了，不过这句还没有补上刚才缺的内容。"


def _provider_acknowledgement(acknowledgement_id: str) -> str:
    return {
        "light_redirect": "我听到了，我们先把刚才没说完的规则补完整",
        "respect_preference": "明白，这代表你的偏好，我不会替你直接定成规则",
        "answer_question": "你是在问这个方向是否合适，我先不把提问当作确认",
        "ask_rephrase": "我还没完全理解这句补充，先保留原来的规则",
        "confirm_cancel": "明白，你暂时不想继续这个方向，我不会改动原规则",
    }[acknowledgement_id]


def _merge_clarification_answer(
    original: str,
    answer: str,
    *,
    diagnostic_code: str,
) -> str:
    supplement = answer.strip(" ，,。；;\n\t")
    base = original.strip(" ，,。；;\n\t")
    if diagnostic_code == "idea_guidance_required":
        return supplement
    if diagnostic_code in {
        "instrument_required",
        "instrument_unconfirmed",
        "instrument_resolution_unavailable",
    }:
        return f"{supplement}，{base}"
    return f"{base}，{supplement}"


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
