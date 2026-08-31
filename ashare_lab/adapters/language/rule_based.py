"""Deterministic Chinese parser for the offline golden set and fallback path.

Buy and sell clauses are interpreted independently.  Indicator defaults may
fill parameters for an indicator the user actually named, but they must never
introduce an unrelated indicator into the opposite action clause.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from ashare_lab.adapters.language.backtest_period import parse_backtest_period
from ashare_lab.domain.market_data import AshareInstrumentCodeError, normalize_a_share_instrument
from ashare_lab.ports.candidate_generation import (
    CandidateAst,
    CompileInput,
    ConditionJoin,
    DocumentTextIntent,
    EventIntent,
    ExitIntent,
    HoldingPeriodIntent,
    IndicatorIntent,
    PositionReturnIntent,
    SignalIntent,
    TrailingDrawdownIntent,
)

_BARE_SYMBOL_PATTERN = r"(?:[03468]\d{5}|920\d{3})"
_SYMBOL_RE = re.compile(
    rf"(?<!\d)({_BARE_SYMBOL_PATTERN})(?:\.(SH|SZ|BJ))?(?!\d)",
    re.IGNORECASE,
)
_NUMBER = r"(\d+(?:\.\d+)?)"
_SIGNED_NUMBER = r"(-?\d+(?:\.\d+)?)"
_ACTION_RE = re.compile(r"(?<!超)(?:买入|卖出|买进|卖掉|(?<!购)买|卖)")
_BOOLEAN_AND_RE = re.compile(r"(?:并且|而且|同时|以及|且)")
_BOOLEAN_OR_RE = re.compile(r"(?:或者|或是|任一|任意(?:一个|一项)?|或)")
_BOOLEAN_GROUP_RE = re.compile(r"[（(][^()（）]*[A-Za-z\u3400-\u9fff][^()（）]*[）)]")
_GENERIC_ENTRY_PLACEHOLDER_RE = re.compile(r"(?:随便|任意|随机|不知道|不确定|你看着)")
_DOCUMENT_METRIC_HINT_RE = re.compile(
    r"(?:正文|全文|关键词|提到|提及|包含|出现|次数|词频|占比|频率)",
    re.IGNORECASE,
)
_HOLDING_PERIOD_HINT_RE = re.compile(
    r"(?:(?:持有|成交后|买入后|买入成交后)[^，。；;]{0,16}"
    r"\d{1,4}(?:个)?(?:交易日|交易天|自然日|天|日)|"
    r"^(?:后)?第?\d{1,4}(?:个)?(?:交易日|交易天|自然日|天|日)(?:后|时|到期|内)?$)"
)
_PREFIXED_HOLDING_PERIOD_RE = re.compile(
    r"(?:(?:买入)?成交后|买入后|持有)(?:第)?(?P<sessions>\d{1,4})(?:个)?"
    r"(?P<unit>交易日|交易天|天|日)(?:后|时|到期)?"
)
_BARE_HOLDING_PERIOD_RE = re.compile(
    r"^(?:后)?第?(?P<sessions>\d{1,4})(?:个)?(?P<unit>交易日|交易天|天|日)"
    r"(?:后|时|到期)?$"
)
_NATURAL_DAY_RE = re.compile(r"\d{1,4}(?:个)?自然日")
_DOCUMENT_TERM = r"(?:[A-Za-z][A-Za-z0-9+.#_-]{0,31}|[\u3400-\u9fff]{1,16})"
_DOCUMENT_COMPARATOR = r"(?:不少于|不低于|大于等于|至少|超过|大于|多于|>=|>|≥)"
_DOCUMENT_TERM_COUNT_PATTERNS = (
    re.compile(
        rf"(?:完整)?(?:年度报告|年报|半年度报告|半年报|中报|季度报告|季报)"
        rf"(?:(?:的)?(?:正文|全文))?(?:中|里)?[\"'“”‘’]?"
        rf"(?P<term>{_DOCUMENT_TERM})[\"'“”‘’]?(?:出现|提及|被提到)?"
        rf"(?P<comparator>{_DOCUMENT_COMPARATOR})(?P<value>\d{{1,7}})次"
    ),
    re.compile(
        rf"(?:完整)?(?:年度报告|年报|半年度报告|半年报|中报|季度报告|季报)"
        rf"(?:(?:的)?(?:正文|全文))?(?:中|里)?[\"'“”‘’]?"
        rf"(?P<term>{_DOCUMENT_TERM})[\"'“”‘’]?"
        rf"(?P<comparator>{_DOCUMENT_COMPARATOR})(?:出现|提及|被提到)"
        r"(?P<value>\d{1,7})次"
    ),
    re.compile(
        rf"(?:提到|提及|包含)[\"'“”‘’]?(?P<term>{_DOCUMENT_TERM})[\"'“”‘’]?"
        rf"(?:这个词|关键词)?(?:的)?(?:出现)?(?:次数?)?(?P<comparator>{_DOCUMENT_COMPARATOR})"
        r"(?P<value>\d{1,7})次"
    ),
)
_EVENT_WORDS = (
    "公告",
    "业绩预告",
    "分红",
    "利润分配",
    "除息",
    "除权",
    "转增",
    "送股",
    "配股",
    "回购",
    "减持",
    "增持",
    "限售",
    "龙虎榜",
    "停牌",
    "复牌",
    "解禁",
    "股权激励",
    "限制性股票",
    "股票期权",
    "行政处罚",
    "立案调查",
    "立案告知书",
    "信披违规",
    "信息披露违规",
    "诉讼",
    "仲裁",
    "债务违约",
    "评级下调",
    "破产重整",
    "董事长",
    "总经理",
    "财务负责人",
    "财务总监",
    "董事会秘书",
    "董事",
    "监事",
    "纪律处分",
    "公开谴责",
    "实际控制人",
    "持股",
    "重组",
    "购买资产",
    "资产出售",
    "分拆上市",
    "合同",
    "订单",
    "框架协议",
    "战略合作",
    "中标",
    "获标",
    "入围",
    "获批",
    "核准",
    "业务许可",
    "许可证",
    "批件",
    "业务资格",
    "同意注册",
)

_NON_FINAL_WEB_EVENT_PHRASES = (
    "中标候选人",
    "候选中标",
    "预中标",
    "入围",
    "未最终中标",
    "尚未最终中标",
    "未正式中标",
    "尚未正式中标",
    "未确定为中标人",
    "没有确定为中标人",
    "未获标",
    "尚未获标",
    "没有获标",
    "未中标",
    "中标失败",
    "落标",
    "受理",
    "待批",
    "待核准",
    "待审批",
    "审批中",
    "审核中",
    "accepted",
    "pending",
)
_FINAL_AWARD_PHRASES = ("最终中标", "正式中标", "确定为中标人", "获标")
_LICENSE_APPROVAL_RE = re.compile(
    r"(?:业务许可|许可证|批件|业务资格)(?:已经|已|正式|明确|获得|得到){0,2}(?:获批|核准)"
)
_WEB_EVENT_CONTEXT_WORDS = (
    "中标",
    "获标",
    "入围",
    "业务许可",
    "许可证",
    "批件",
    "业务资格",
)
_NEGATED_EVENT_STAGE_RE = re.compile(
    r"(?:尚未|没有|未|不)(?:能|再)?(?:最终|正式|明确)?"
    r"(?:获得|取得|获批|核准|批准|同意注册|审议通过|发布|披露|完成|完毕|"
    r"终止|停止|实施|授予|签署|签订|收到|发生|受理|达到|降至|变更|辞职|"
    r"辞任|下调|注销|除权|除息)"
)
_DENIED_OR_CLARIFICATION_EVENT_RE = re.compile(
    r"(?:传闻不实|不属实|不存在|予以澄清|澄清(?:公告|说明)?)"
)

# Rules are ordered by lifecycle specificity.  Every pattern names both an
# event family and a concrete stage; broad words such as ``回购`` or ``重组``
# are intentionally absent and therefore fail closed in ``_parse_clause``.
_ANNOUNCEMENT_EVENT_DEFINITIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Dividends.
    (
        re.compile(r"(?:分红|现金分红).*(?:股东大会审议通过|获批)"),
        "event.dividends_corporate_actions.cash_dividend_approved",
    ),
    (
        re.compile(r"(?:分红除息|现金分红除息|现金红利(?:发放日|除息日))"),
        "event.dividends_corporate_actions.cash_dividend_ex_date",
    ),
    (
        re.compile(r"(?:现金)?分红(?:预案|方案)"),
        "event.dividends_corporate_actions.cash_dividend_proposal",
    ),
    (
        re.compile(
            r"(?:资本公积|资本公积金).*(?:转增股本|转增股份).*(?:预案|方案)|(?:资本公积|资本公积金)转增股本"
        ),
        "event.dividends_corporate_actions.capitalization_issue",
    ),
    (
        re.compile(r"配股(?:发行)?(?:预案|方案)|(?:预案|方案).*配股"),
        "event.dividends_corporate_actions.rights_issue",
    ),
    (
        re.compile(r"送股.*(?:除权|除权日)|(?:除权|除权日).*送股"),
        "event.dividends_corporate_actions.stock_dividend_ex_date",
    ),
    (
        re.compile(r"送股(?:预案|方案)|(?:预案|方案).*送股"),
        "event.dividends_corporate_actions.stock_dividend_proposal",
    ),
    # Restricted shares and ownership changes.
    (
        re.compile(
            r"(?:限售股|限售股份).*解禁.*(?:安排|日期).*(?:变更|调整)|(?:变更|调整).*(?:限售股|限售股份).*解禁"
        ),
        "event.restricted_shares_pledges.unlock_schedule_change",
    ),
    (
        re.compile(r"(?:限售股|限售股份).*(?:解禁|上市流通)|解除限售.*上市流通"),
        "event.restricted_shares_pledges.restricted_shares_unlock",
    ),
    (
        re.compile(r"实际控制人.*(?:发生)?变更|(?:变更|成为).*实际控制人"),
        "event.shareholder_holdings.actual_controller_change",
    ),
    (
        re.compile(
            r"(?:董事|监事|高级管理人员|董监高).*减持.*(?:完成|进展|结果|实施)|(?:董监高)减持"
        ),
        "event.shareholder_holdings.executive_decrease",
    ),
    (
        re.compile(
            r"(?:董事|监事|高级管理人员|董监高).*增持.*(?:完成|进展|结果|实施)|(?:董监高)增持"
        ),
        "event.shareholder_holdings.executive_increase",
    ),
    (
        re.compile(r"持股(?:比例)?.*(?:降至|低于|<)5%|5%以下"),
        "event.shareholder_holdings.ownership_below_five_percent",
    ),
    (
        re.compile(r"持股(?:比例)?.*(?:达到|超过|升至|>=?)5%"),
        "event.shareholder_holdings.ownership_reaches_five_percent",
    ),
    (
        re.compile(
            r"(?:股东|控股股东|实际控制人).*减持.*(?:进展|完成|完毕|结果)|减持计划.*(?:进展|完成|完毕|结果)"
        ),
        "event.shareholder_holdings.major_holder_decrease_progress",
    ),
    (
        re.compile(
            r"(?:股东|控股股东|实际控制人).*减持.*(?:计划|预披露)|减持股份预披露|减持计划公告"
        ),
        "event.shareholder_holdings.major_holder_decrease_plan",
    ),
    (
        re.compile(
            r"(?:股东|控股股东|实际控制人).*增持.*(?:进展|完成|完毕|结果)|增持计划.*(?:进展|完成|完毕|结果)"
        ),
        "event.shareholder_holdings.major_holder_increase_progress",
    ),
    (
        re.compile(r"(?:股东|控股股东|实际控制人).*增持.*(?:计划|拟增持)|增持计划公告"),
        "event.shareholder_holdings.major_holder_increase_plan",
    ),
    # Repurchase lifecycle.
    (
        re.compile(r"回购股份.*注销|注销.*回购股份"),
        "event.repurchase_capital.repurchase_cancellation",
    ),
    (
        re.compile(
            r"(?:变更|调整).*回购(?:股份)?(?:预案|方案)|回购(?:股份)?(?:预案|方案).*(?:变更|调整)"
        ),
        "event.repurchase_capital.repurchase_change",
    ),
    (
        re.compile(r"(?:终止|停止).*回购|回购.*(?:终止|停止)"),
        "event.repurchase_capital.repurchase_termination",
    ),
    (
        re.compile(r"回购股份.*(?:实施结果|实施完成|完成公告)|完成回购.*股份"),
        "event.repurchase_capital.repurchase_completion",
    ),
    (
        re.compile(r"首次(?:实施)?回购.*股份|首次回购公司股份"),
        "event.repurchase_capital.repurchase_first_execution",
    ),
    (
        re.compile(r"回购股份.*(?:进展|比例达到)|回购.*进展公告"),
        "event.repurchase_capital.repurchase_progress",
    ),
    (
        re.compile(r"股东大会.*(?:审议通过|批准).*回购|回购.*(?:股东大会审议通过|获股东大会批准)"),
        "event.repurchase_capital.repurchase_approved",
    ),
    (
        re.compile(r"回购(?:公司)?股份.*(?:预案|方案)"),
        "event.repurchase_capital.repurchase_proposal",
    ),
    # Equity incentives.
    (
        re.compile(
            r"(?:股权|限制性股票|股票期权)激励计划.*(?:首次授予|预留授予|授予登记|授予完成)|向.*授予.*(?:限制性股票|股票期权)"
        ),
        "event.governance_personnel.equity_incentive_grant",
    ),
    (
        re.compile(r"(?:股权|限制性股票|股票期权)激励计划.*(?:草案|方案)"),
        "event.governance_personnel.equity_incentive_plan",
    ),
    # Regulation, litigation and credit risk.
    (
        re.compile(r"行政处罚决定书|收到行政处罚(?!事先告知)"),
        "event.regulation_risk.administrative_penalty",
    ),
    (
        re.compile(r"(?:收到)?立案告知书|被立案调查|监管立案调查"),
        "event.regulation_risk.investigation_opened",
    ),
    (
        re.compile(r"(?:信披|信息披露)(?:违法违规|违规)(?:认定|决定|结论)"),
        "event.regulation_risk.information_disclosure_violation",
    ),
    (
        re.compile(r"(?:纪律处分|通报批评)(?:决定|处分决定|决定书)|收到纪律处分决定"),
        "event.regulation_risk.disciplinary_action",
    ),
    (
        re.compile(r"公开谴责(?:决定|决定书)|收到公开谴责"),
        "event.regulation_risk.public_censure",
    ),
    (
        re.compile(r"(?:重大诉讼|诉讼事项).*(?:进展|判决|裁决|结果)"),
        "event.litigation_credit.litigation_progress",
    ),
    (
        re.compile(r"(?:涉及|新增|发生)?重大诉讼(?!.*(?:进展|判决|裁决|结果))"),
        "event.litigation_credit.major_litigation",
    ),
    (
        re.compile(r"(?:涉及|新增|发生)?重大仲裁|仲裁机构.*(?:受理|裁决)"),
        "event.litigation_credit.arbitration",
    ),
    (
        re.compile(r"(?:债务|贷款|债券).*(?:逾期|违约|未能按期兑付)|未能按期兑付.*(?:债券|本息)"),
        "event.litigation_credit.debt_default",
    ),
    (
        re.compile(r"(?:主体|债项|信用)评级.*(?:下调|调降)|评级下调"),
        "event.litigation_credit.credit_rating_downgrade",
    ),
    (
        re.compile(
            r"(?:申请|受理|进入).*(?:破产重整|破产清算)|(?:破产重整|破产清算).*(?:申请|受理|进展)"
        ),
        "event.litigation_credit.bankruptcy_reorganization",
    ),
    # Governance changes.
    (
        re.compile(r"董事长.*(?:辞职|辞任|离任|变更|选举|选定)|(?:选举|选定|变更).*董事长"),
        "event.governance_personnel.chairman_change",
    ),
    (
        re.compile(
            r"(?:^|[^副])总经理.*(?:辞职|辞任|离任|变更)|(?:聘任|任命|变更).*(?:^|[^副])总经理"
        ),
        "event.governance_personnel.ceo_change",
    ),
    (
        re.compile(
            r"(?:财务负责人|财务总监).*(?:辞职|辞任|离任|变更)|(?:聘任|任命|变更).*(?:财务负责人|财务总监)"
        ),
        "event.governance_personnel.cfo_change",
    ),
    (
        re.compile(r"董事会秘书.*(?:辞职|辞任|离任|变更)|(?:聘任|任命|变更).*董事会秘书"),
        "event.governance_personnel.board_secretary_change",
    ),
    (
        re.compile(r"(?:公司)?董事(?!长|会秘书).*(?:辞职|辞任|离任)"),
        "event.governance_personnel.director_resignation",
    ),
    (
        re.compile(r"(?:公司)?监事.*(?:辞职|辞任|离任)"),
        "event.governance_personnel.supervisor_resignation",
    ),
    # M&A lifecycle.
    (
        re.compile(
            r"终止.*(?:重大资产重组|发行股份购买资产)|(?:重大资产重组|发行股份购买资产).*终止"
        ),
        "event.m_and_a_restructuring.restructuring_terminated",
    ),
    (
        re.compile(
            r"(?:重大资产重组|发行股份购买资产).*(?:实施完成|交割完成)|(?:实施完成|交割完成).*(?:重大资产重组|发行股份购买资产)"
        ),
        "event.m_and_a_restructuring.restructuring_completed",
    ),
    (
        re.compile(
            r"(?:重大资产重组|发行股份购买资产).*(?:获|取得).*(?:证监会|交易所).*(?:同意|批准|注册)|(?:证监会|交易所).*(?:同意|批准|注册).*(?:重大资产重组|发行股份购买资产)"
        ),
        "event.m_and_a_restructuring.restructuring_regulatory_approval",
    ),
    (
        re.compile(
            r"股东大会.*审议通过.*(?:重大资产重组|发行股份购买资产)|(?:重大资产重组|发行股份购买资产).*股东大会审议通过"
        ),
        "event.m_and_a_restructuring.restructuring_approved_shareholders",
    ),
    (
        re.compile(
            r"董事会.*审议通过.*(?:重大资产重组|发行股份购买资产)|(?:重大资产重组|发行股份购买资产).*董事会审议通过"
        ),
        "event.m_and_a_restructuring.restructuring_approved_board",
    ),
    (
        re.compile(r"(?:重大资产重组|筹划重组).*复牌|复牌.*(?:重大资产重组|筹划重组)"),
        "event.m_and_a_restructuring.restructuring_resumption",
    ),
    (
        re.compile(r"(?:重大资产重组|筹划重组).*停牌|停牌.*(?:重大资产重组|筹划重组)"),
        "event.m_and_a_restructuring.restructuring_suspension",
    ),
    (
        re.compile(r"分拆.*上市"),
        "event.m_and_a_restructuring.spin_off_listing",
    ),
    (
        re.compile(r"(?:重大资产出售|出售重大资产).*(?:预案|方案)"),
        "event.m_and_a_restructuring.asset_sale_plan",
    ),
    (
        re.compile(r"(?:发行股份购买资产|重大资产收购|收购重大资产).*(?:预案|方案)"),
        "event.m_and_a_restructuring.acquisition_plan",
    ),
    # Material contracts and orders.
    (
        re.compile(
            r"(?:重大合同|重大经营合同).*(?:终止|解除)|(?:终止|解除).*(?:重大合同|重大经营合同)"
        ),
        "event.contracts_orders.contract_terminated",
    ),
    (
        re.compile(
            r"(?:重大合同|重大经营合同).*(?:变更|调整)|(?:变更|调整).*(?:重大合同|重大经营合同)"
        ),
        "event.contracts_orders.contract_changed",
    ),
    (
        re.compile(r"(?:重大合同|重大经营合同).*(?:履行完毕|完成履行|完成交付)"),
        "event.contracts_orders.contract_completed",
    ),
    (
        re.compile(r"(?:重大合同|重大经营合同).*(?:进展|履行情况)"),
        "event.contracts_orders.contract_progress",
    ),
    (
        re.compile(r"重大项目.*(?:中标|中选)|(?:中标|中选).*重大项目"),
        "event.contracts_orders.major_contract_won",
    ),
    (
        re.compile(
            r"(?:签署|签订).*(?:重大合同|重大经营合同)|(?:重大合同|重大经营合同).*(?:签署|签订)"
        ),
        "event.contracts_orders.major_contract_signed",
    ),
    (
        re.compile(r"(?:签署|签订).*(?:战略合作|框架)协议|(?:战略合作|框架)协议.*(?:签署|签订)"),
        "event.contracts_orders.framework_agreement",
    ),
    (
        re.compile(r"(?:收到|获得|取得).*重大订单|重大订单.*(?:收到|获得|取得)"),
        "event.contracts_orders.purchase_order_received",
    ),
)

type _Action = Literal["entry", "exit"]
type _Family = Literal[
    "adx",
    "atr",
    "bias",
    "macd",
    "rsi",
    "ma",
    "ema",
    "ma_cross",
    "bollinger",
    "kdj",
    "cci",
    "bbi",
    "ema_bias",
    "dmi",
    "donchian",
    "historical_volatility",
    "return_pct",
    "return_stddev",
    "roc",
    "momentum",
    "natr",
    "rolling_high",
    "consecutive_up",
    "amplitude",
    "amount",
    "amount_average",
    "obv",
    "trend_regime",
    "volume_divergence",
    "volume_price",
    "relative_volume",
    "stochastic",
    "true_range",
    "williams_r",
]

_CROSS_FAMILIES: frozenset[_Family] = frozenset({"macd", "ma_cross", "kdj"})


@dataclass(frozen=True, slots=True)
class _Clause:
    text: str
    action: _Action


class RuleBasedCandidateGenerator:
    """Produce only catalog-addressable intents; never emit executable code."""

    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        text = _normalize(request.utterance)
        mentioned_symbol = _extract_symbol(text)
        context_symbol = _normalize_context(request.instrument_context)
        if (_SYMBOL_RE.search(text) is not None and mentioned_symbol is None) or (
            request.instrument_context is not None and context_symbol is None
        ):
            return (_unsupported(None, "invalid_a_share_instrument", 1.0),)
        if (
            context_symbol is not None
            and mentioned_symbol is not None
            and mentioned_symbol != context_symbol
        ):
            return (_unsupported(context_symbol, "instrument_context_mismatch", 1.0),)
        symbol = context_symbol or mentioned_symbol
        backtest_period = parse_backtest_period(request.utterance)
        if backtest_period.diagnostic_code is not None:
            return (_unsupported(symbol, backtest_period.diagnostic_code, 1.0),)
        if "大跌反弹" in text:
            return (_unsupported(symbol, "template_not_published/big_drop_rebound", 1.0),)

        clauses = _action_clauses(text)
        families = _explicit_families(text)
        if not clauses and (families or _event_intent(text) is not None):
            return (_unsupported(symbol, "strategy_rule_incomplete", 0.8),)
        if not clauses:
            return (_unsupported(symbol, "no_supported_signal_recognized", 0.0),)

        entry: list[SignalIntent] = []
        exit_: list[ExitIntent] = []
        entry_join: ConditionJoin | None = None
        exit_join: ConditionJoin | None = None
        entry_rule_clause_count = 0
        exit_rule_clause_count = 0
        defaulted: list[str] = []
        for clause in clauses:
            intents, clause_defaults, unsupported_code = _parse_clause(
                clause,
                full_text=text,
                global_families=families,
            )
            if unsupported_code is not None:
                return (_unsupported(symbol, unsupported_code, 0.98),)
            clause_join, join_error = _condition_join(clause.text, intents)
            if join_error is not None:
                return (_unsupported(symbol, join_error, 0.98),)
            if clause.action == "entry":
                if clause_join is not None and entry_join is not None and clause_join != entry_join:
                    return (_unsupported(symbol, "ambiguous_boolean_expression", 0.98),)
                entry_join = clause_join or entry_join
                if any(isinstance(item, HoldingPeriodIntent) for item in intents):
                    return (_unsupported(symbol, "holding_period_entry_not_supported", 1.0),)
                signal_intents = tuple(
                    item for item in intents if isinstance(item, (IndicatorIntent, EventIntent))
                )
                if signal_intents:
                    entry_rule_clause_count += 1
                    entry.extend(signal_intents)
            else:
                if clause_join is not None and exit_join is not None and clause_join != exit_join:
                    return (_unsupported(symbol, "ambiguous_boolean_expression", 0.98),)
                exit_join = clause_join or exit_join
                if intents:
                    exit_rule_clause_count += 1
                exit_.extend(intents)
            defaulted.extend(clause_defaults)

        if entry_rule_clause_count > 1 or exit_rule_clause_count > 1:
            return (_unsupported(symbol, "ambiguous_boolean_expression", 0.98),)

        entry_clauses = tuple(clause for clause in clauses if clause.action == "entry")
        has_generic_entry_placeholder = any(
            _GENERIC_ENTRY_PLACEHOLDER_RE.search(clause.text) is not None
            for clause in entry_clauses
        )
        if not entry and exit_ and (not entry_clauses or has_generic_entry_placeholder):
            return (_unsupported(symbol, "entry_rule_not_recognized", 0.7),)
        if not entry:
            return (_unsupported(symbol, "no_supported_signal_recognized", 0.0),)
        if not exit_:
            return (_unsupported(symbol, "exit_rule_not_recognized", 0.7),)
        return (
            CandidateAst(
                instrument_symbol=symbol,
                entry=tuple(entry),
                exit=tuple(exit_),
                confidence=0.99,
                entry_join=entry_join or "all",
                exit_join=exit_join or "any",
                defaulted_fields=tuple(sorted(set(defaulted))),
                backtest_start=backtest_period.start,
                backtest_end=backtest_period.end,
                backtest_lookback_years=backtest_period.lookback_years,
            ),
        )


def _condition_join(
    text: str,
    intents: list[ExitIntent],
) -> tuple[ConditionJoin | None, str | None]:
    has_and = _BOOLEAN_AND_RE.search(text) is not None
    has_or = _BOOLEAN_OR_RE.search(text) is not None
    if has_and and has_or:
        return None, "ambiguous_boolean_expression"
    if (has_and or has_or) and (len(intents) < 2 or _BOOLEAN_GROUP_RE.search(text) is not None):
        return None, "ambiguous_boolean_expression"
    if has_and:
        return "all", None
    if has_or:
        return "any", None
    if len(intents) > 1 and not _is_intrinsic_multi_intent(intents):
        return None, "ambiguous_boolean_expression"
    return None, None


def _is_intrinsic_multi_intent(intents: list[ExitIntent]) -> bool:
    if not intents or not all(isinstance(intent, IndicatorIntent) for intent in intents):
        return False
    indicator_ids = {
        intent.indicator_id for intent in intents if isinstance(intent, IndicatorIntent)
    }
    return len(indicator_ids) == 1 and indicator_ids <= {
        "technical.kdj",
        "technical.trend_regime",
    }


def _parse_clause(
    clause: _Clause,
    *,
    full_text: str,
    global_families: frozenset[_Family],
) -> tuple[list[ExitIntent], list[str], str | None]:
    text = clause.text
    action = clause.action
    intents: list[ExitIntent] = []
    defaulted: list[str] = []
    clause_families = _explicit_families(text)

    event_intent = _event_intent(text)
    if event_intent is not None:
        intents.append(event_intent)
        if event_intent.document_text is None and _DOCUMENT_METRIC_HINT_RE.search(text) is not None:
            return [], [], "event_document_metric_not_supported"
    elif any(word in text for word in _EVENT_WORDS):
        return [], [], "event_not_executable"

    if action == "exit":
        if _NATURAL_DAY_RE.search(text) is not None:
            return [], [], "natural_day_holding_period_requires_clarification"
        holding_period = _holding_period_intent(text)
        if holding_period is not None:
            sessions, unit_was_defaulted = holding_period
            intents.append(HoldingPeriodIntent(sessions=sessions))
            if unit_was_defaulted:
                defaulted.append("/exit/holding_period/count_mode")
        elif _HOLDING_PERIOD_HINT_RE.search(text) is not None:
            return [], [], "holding_period_exit_not_supported"

    risk_intents, risk_error = _explicit_risk_exit_intents(text, action)
    if risk_error is not None:
        return [], [], risk_error
    intents.extend(risk_intents)

    negation_error = _technical_negation_error(text, clause_families, global_families)
    if negation_error is not None:
        return [], [], negation_error

    cross_family, cross_error = _resolve_cross_family(text, clause_families, global_families)
    if cross_error is not None:
        return [], [], cross_error

    if "macd" in clause_families or cross_family == "macd":
        macd_params = _macd_params(text) or _macd_params(full_text)
        if macd_params is None and _has_explicit_call(full_text, "macd"):
            return [], [], "unsupported_macd_parameters"
        fast, slow, signal = macd_params or (12, 26, 9)
        trigger = _macd_trigger(text, action)
        if trigger is None:
            return [], [], "ambiguous_macd_trigger"
        intents.append(
            _intent(
                "technical.macd",
                trigger,
                params=(("fast", fast), ("signal", signal), ("slow", slow)),
            )
        )
        if not re.search(r"(?:金叉|死叉|(?:零|0)轴|dif|dea|上穿|下穿|突破|跌破)", text, re.I):
            defaulted.append(_default_path(action, "macd", "trigger"))

    if _family_active("rsi", text, clause_families, global_families):
        rsi_period = _rsi_period(text) or _rsi_period(full_text)
        if rsi_period is None and _has_explicit_call(full_text, "rsi"):
            return [], [], "unsupported_rsi_parameters"
        threshold_rule = _threshold_rule(text)
        if threshold_rule is None:
            if _has_unsupported_numeric_relation(text):
                return [], [], "unsupported_comparator"
            if _has_unmodeled_direction(text):
                return [], [], "unsupported_rsi_direction"
            if "超卖" in text:
                threshold_rule = (30.0, "below")
            elif "超买" in text:
                threshold_rule = (70.0, "above")
            else:
                threshold_rule = (30.0, "below") if action == "entry" else (70.0, "above")
            defaulted.append(_default_path(action, "rsi", "value"))
        intents.append(
            _intent(
                "technical.rsi",
                threshold_rule[1],
                params=(("period", rsi_period or 14),),
                value=threshold_rule[0],
            )
        )

    if "ma_cross" in clause_families or cross_family == "ma_cross":
        relation = _ma_pair_relation(text) or _ma_pair_relation(full_text)
        if relation is None:
            fast_period, slow_period = 5, 20
            defaulted.extend(
                (
                    _default_path(action, "ma_cross", "fast_period"),
                    _default_path(action, "ma_cross", "slow_period"),
                )
            )
        else:
            fast_period, slow_period, _ = relation
        trigger = _ma_cross_trigger(text, action)
        intents.append(
            _intent(
                "technical.ma_cross",
                trigger,
                params=(
                    ("fast_period", fast_period),
                    ("price_field", "close"),
                    ("slow_period", slow_period),
                ),
            )
        )

    if _family_active("ema_bias", text, clause_families, global_families):
        threshold_rule = _threshold_rule(text)
        if threshold_rule is None:
            if _has_unsupported_numeric_relation(text):
                return [], [], "unsupported_comparator"
            threshold_rule = (-5.0, "below") if action == "entry" else (5.0, "above")
            defaulted.append(_default_path(action, "ema_bias", "value"))
        period = _ema_period(text) or _ema_period(full_text) or 28
        intents.append(
            _intent(
                "technical.ema_bias",
                threshold_rule[1],
                params=(("period", period), ("price_field", "close")),
                value=threshold_rule[0],
            )
        )

    if _family_active("ema", text, clause_families, global_families):
        if _has_unmodeled_direction(text) and not _has_price_operator(text):
            return [], [], "unsupported_ema_direction"
        period = _ema_period(text) or _ema_period(full_text) or 28
        intents.append(
            _intent(
                "technical.ema",
                _price_trigger(text, action),
                params=(("period", period), ("price_field", "close")),
            )
        )

    if _family_active("ma", text, clause_families, global_families):
        if _has_unmodeled_direction(text) and not _has_price_operator(text):
            return [], [], "unsupported_ma_direction"
        period = _ma_price_period(text) or _ma_price_period(full_text) or 20
        intents.append(
            _intent(
                "technical.ma",
                _price_trigger(text, action),
                params=(("period", period), ("price_field", "close")),
            )
        )

    if _family_active("bollinger", text, clause_families, global_families):
        bollinger_trigger = _bollinger_trigger(text)
        if bollinger_trigger is not None:
            intents.append(
                _intent(
                    "technical.bollinger",
                    bollinger_trigger,
                    params=(
                        ("period", _bollinger_period(text) or _bollinger_period(full_text) or 20),
                        ("price_field", "close"),
                        ("stddev_multiplier", 2.0),
                    ),
                )
            )

    if (
        "kdj" in clause_families
        or cross_family == "kdj"
        or _inherits_j_rule(text, clause_families, global_families)
    ):
        parsed_kdj_params = _kdj_params(text) or _kdj_params(full_text)
        if parsed_kdj_params is None and _has_explicit_call(full_text, "kdj"):
            return [], [], "unsupported_kdj_parameters"
        period, k_smoothing, d_smoothing = parsed_kdj_params or (9, 3, 3)
        kdj_params = (
            ("d_smoothing", d_smoothing),
            ("k_smoothing", k_smoothing),
            ("period", period),
        )
        kdj_line_trigger = _kdj_line_cross_trigger(text)
        if ("超买" in text or "超卖" in text) and not _mentions_kdj_j_value(text):
            return [], [], "unsupported_kdj_overbought_semantics"
        if _has_unmodeled_direction(text) and kdj_line_trigger is None:
            return [], [], "unsupported_kdj_direction"
        has_cross_request = (
            "金叉" in text
            or "死叉" in text
            or cross_family == "kdj"
            or kdj_line_trigger is not None
            or _mentions_incomplete_kdj_cross(text)
        )
        if has_cross_request:
            trigger = _kdj_cross_trigger(text, action)
            if trigger is None:
                return [], [], "ambiguous_kdj_trigger"
            intents.append(_intent("technical.kdj", trigger, params=kdj_params))
        j_rule = (
            _threshold_rule(text)
            if _mentions_kdj_j_value(text) or re.search(r"(?:低位|高位)", text)
            else None
        )
        if j_rule is not None:
            intents.append(
                _intent(
                    "technical.kdj",
                    "j_above" if j_rule[1] in {"above", "crosses_above"} else "j_below",
                    params=kdj_params,
                    value=j_rule[0],
                )
            )
        elif "低位" in text and ("金叉" in text or action == "entry"):
            intents.append(_intent("technical.kdj", "j_below", params=kdj_params, value=20))
            defaulted.append(_default_path(action, "kdj", "j_threshold"))
        elif "高位" in text and ("死叉" in text or action == "exit"):
            intents.append(_intent("technical.kdj", "j_above", params=kdj_params, value=80))
            defaulted.append(_default_path(action, "kdj", "j_threshold"))
        if not any(
            isinstance(item, IndicatorIntent) and item.indicator_id == "technical.kdj"
            for item in intents
        ):
            trigger = _kdj_cross_trigger(text, action)
            if trigger is None:
                return [], [], "ambiguous_kdj_trigger"
            intents.append(_intent("technical.kdj", trigger, params=kdj_params))
            defaulted.append(_default_path(action, "kdj", "trigger"))

    if _family_active("cci", text, clause_families, global_families):
        threshold_rule = _threshold_rule(text)
        if threshold_rule is None:
            if _has_unsupported_numeric_relation(text):
                return [], [], "unsupported_comparator"
            if _has_unmodeled_direction(text):
                return [], [], "unsupported_cci_direction"
            if "超卖" in text:
                threshold_rule = (-100.0, "below")
            elif "超买" in text:
                threshold_rule = (100.0, "above")
            else:
                threshold_rule = (-100.0, "below") if action == "entry" else (100.0, "above")
            defaulted.append(_default_path(action, "cci", "value"))
        intents.append(
            _intent(
                "technical.cci",
                threshold_rule[1],
                params=(("constant", 0.015), ("period", _cci_period(text) or 14)),
                value=threshold_rule[0],
            )
        )

    if _family_active("bbi", text, clause_families, global_families):
        if _has_unmodeled_direction(text) and not _has_price_operator(text):
            return [], [], "unsupported_bbi_direction"
        periods = _bbi_periods(text) or _bbi_periods(full_text) or (3, 6, 12, 24)
        intents.append(
            _intent(
                "technical.bbi",
                _price_trigger(text, action),
                params=(
                    ("period_1", periods[0]),
                    ("period_2", periods[1]),
                    ("period_3", periods[2]),
                    ("period_4", periods[3]),
                    ("price_field", "close"),
                ),
            )
        )

    p1_threshold_families: tuple[
        tuple[_Family, str, tuple[tuple[str, str | int | float | bool], ...]], ...
    ] = (
        ("atr", "technical.atr", (("period", _p1_period(text, full_text, "atr", 14)),)),
        ("natr", "technical.natr", (("period", _p1_period(text, full_text, "natr", 14)),)),
        ("adx", "technical.adx", (("period", _p1_period(text, full_text, "adx", 14)),)),
        (
            "bias",
            "technical.bias",
            (
                ("period", _p1_period(text, full_text, "bias", 20)),
                ("price_field", "close"),
            ),
        ),
        (
            "roc",
            "technical.roc",
            (
                ("period", _p1_period(text, full_text, "roc", 12)),
                ("price_field", "close"),
            ),
        ),
        (
            "momentum",
            "technical.momentum",
            (
                ("period", _p1_period(text, full_text, "momentum", 10)),
                ("price_field", "close"),
            ),
        ),
        (
            "williams_r",
            "technical.williams_r",
            (("period", _p1_period(text, full_text, "williams_r", 14)),),
        ),
        (
            "return_stddev",
            "technical.return_stddev",
            (
                ("period", _p1_period(text, full_text, "return_stddev", 20)),
                ("price_field", "close"),
            ),
        ),
        (
            "historical_volatility",
            "technical.historical_volatility",
            (
                ("annualization_sessions", 252),
                ("period", _p1_period(text, full_text, "historical_volatility", 20)),
                ("price_field", "close"),
            ),
        ),
        ("true_range", "price.true_range", ()),
    )
    for family, indicator_id, params in p1_threshold_families:
        if not _family_active(family, text, clause_families, global_families):
            continue
        threshold_rule = _threshold_rule(text)
        if threshold_rule is None:
            if _has_unsupported_numeric_relation(text):
                return [], [], "unsupported_comparator"
            return [], [], f"ambiguous_{family}_threshold"
        intents.append(
            _intent(
                indicator_id,
                threshold_rule[1],
                params=params,
                value=threshold_rule[0],
            )
        )

    inherits_dmi = not clause_families and global_families == frozenset({"dmi"})
    if "dmi" in clause_families or inherits_dmi:
        dmi_trigger = _dmi_trigger(text)
        if dmi_trigger is None:
            return [], [], "ambiguous_dmi_direction"
        intents.append(
            _intent(
                "technical.dmi",
                dmi_trigger,
                params=(("period", _p1_period(text, full_text, "dmi", 14)),),
            )
        )

    inherits_stochastic = not clause_families and global_families == frozenset({"stochastic"})
    if "stochastic" in clause_families or inherits_stochastic:
        stochastic_rule = _stochastic_rule(text)
        if stochastic_rule is None:
            return [], [], "ambiguous_stochastic_direction"
        trigger, value = stochastic_rule
        intents.append(
            _intent(
                "technical.stochastic",
                trigger,
                params=(
                    ("d_period", _p1_stochastic_period(text, full_text, "d", 3)),
                    ("k_period", _p1_stochastic_period(text, full_text, "k", 14)),
                ),
                value=value,
            )
        )

    inherits_donchian = not clause_families and global_families == frozenset({"donchian"})
    if "donchian" in clause_families or inherits_donchian:
        donchian_trigger = _donchian_trigger(text)
        if donchian_trigger is None:
            return [], [], "ambiguous_donchian_breakout"
        intents.append(
            _intent(
                "technical.donchian",
                donchian_trigger,
                params=(("period", _p1_period(text, full_text, "donchian", 20)),),
            )
        )

    if _family_active("return_pct", text, clause_families, global_families):
        threshold_rule = _return_threshold_rule(text)
        if threshold_rule is None and _has_unsupported_numeric_relation(text):
            return [], [], "unsupported_comparator"
        if threshold_rule is not None:
            intents.append(
                _intent(
                    "price.return_pct",
                    threshold_rule[1],
                    params=(
                        ("period", _return_period(text) or _return_period(full_text) or 5),
                        ("price_field", "close"),
                    ),
                    value=threshold_rule[0],
                )
            )

    if "rolling_high" in clause_families:
        intents.append(
            _intent(
                "price.rolling_high",
                "new_high",
                params=(
                    ("period", _rolling_high_period(text) or 20),
                    ("price_field", "close"),
                ),
            )
        )

    inherits_consecutive_up = (
        not clause_families
        and global_families == frozenset({"consecutive_up"})
        and _mentions_consecutive_up_rule(text)
    )
    if "consecutive_up" in clause_families or inherits_consecutive_up:
        consecutive_days, consecutive_error = _consecutive_up_rule(text)
        if consecutive_error is not None:
            return [], [], consecutive_error
        if consecutive_days is None:
            consecutive_days = 3
            defaulted.append(_default_path(action, "consecutive_up", "days"))
        intents.append(
            _intent(
                "price.consecutive_up",
                "at_least",
                params=(("days", consecutive_days),),
            )
        )

    if _family_active("amplitude", text, clause_families, global_families):
        threshold_rule = _threshold_rule(text)
        if threshold_rule is None and _has_unsupported_numeric_relation(text):
            return [], [], "unsupported_comparator"
        if threshold_rule is not None:
            intents.append(
                _intent(
                    "price.amplitude",
                    threshold_rule[1],
                    params=(),
                    value=threshold_rule[0],
                )
            )

    if _family_active("amount_average", text, clause_families, global_families):
        threshold_rule = _amount_threshold_rule(text)
        if threshold_rule is None and _has_unsupported_numeric_relation(text):
            return [], [], "unsupported_comparator"
        if threshold_rule is not None:
            intents.append(
                _intent(
                    "amount.average",
                    threshold_rule[1],
                    params=(("period", _amount_average_period(text) or 20),),
                    value=threshold_rule[0],
                )
            )

    if _family_active("amount", text, clause_families, global_families):
        threshold_rule = _amount_threshold_rule(text)
        if threshold_rule is None and _has_unsupported_numeric_relation(text):
            return [], [], "unsupported_comparator"
        if threshold_rule is not None:
            intents.append(
                _intent(
                    "market.amount",
                    threshold_rule[1],
                    params=(),
                    value=threshold_rule[0],
                )
            )

    inherits_volume_divergence = (
        not clause_families
        and global_families == frozenset({"volume_divergence"})
        and re.search(r"(?:顶背离|底背离)", text) is not None
    )
    if "volume_divergence" in clause_families or inherits_volume_divergence:
        intents.append(
            _intent(
                "volume.price_divergence",
                "bearish" if "顶背离" in text else "bullish",
                params=(
                    ("average_volume_period", 20),
                    ("left_bars", 3),
                    ("max_separation", 60),
                    ("min_separation", 5),
                    ("obv_threshold_adv", 1.0),
                    ("price_threshold_pct", 2.0),
                    ("right_bars", 3),
                ),
            )
        )

    inherits_obv = (
        not clause_families
        and global_families == frozenset({"obv"})
        and re.search(r"(?:上升|上行|走高|转强|下降|下行|走低|转弱)", text) is not None
    )
    if ("obv" in clause_families or inherits_obv) and "volume_divergence" not in clause_families:
        if re.search(r"(?:上升|上行|走高|转强|下降|下行|走低|转弱)", text) is None:
            return [], [], "ambiguous_obv_direction"
        intents.append(
            _intent(
                "technical.obv",
                "falling" if re.search(r"(?:下降|下行|走低|转弱)", text) else "rising",
                params=(),
            )
        )

    inherits_trend_regime = (
        not clause_families
        and global_families == frozenset({"trend_regime"})
        and re.search(
            r"(?:上涨|上升|上行|向上|多头|转强|下跌|下降|下行|向下|空头|转弱|震荡|横盘|盘整)",
            text,
        )
        is not None
    )
    if "trend_regime" in clause_families or inherits_trend_regime:
        if "转弱" in text:
            triggers = ("range", "downtrend")
        elif re.search(r"(?:下跌|下降|下行|向下|空头)", text):
            triggers = ("downtrend",)
        elif re.search(r"(?:震荡|横盘|盘整)", text):
            triggers = ("range",)
        else:
            triggers = ("uptrend",)
        for trigger in triggers:
            intents.append(
                _intent(
                    "technical.trend_regime",
                    trigger,
                    params=(
                        ("adx_period", 14),
                        ("adx_threshold", 25.0),
                        ("confirmation_days", 2),
                        ("long_period", 60),
                        ("short_period", 20),
                        ("slope_lookback", 5),
                        ("stability_bars", 120),
                    ),
                )
            )

    inherits_volume_price = (
        not clause_families
        and global_families == frozenset({"volume_price"})
        and re.search(r"(?:齐升|齐跌|上涨|下跌|大涨|大跌)", text) is not None
    )
    if "volume_price" in clause_families or inherits_volume_price:
        baseline_period = _volume_baseline_period(text) or 20
        return_threshold = _price_move_threshold(text) or 5.0
        volume_multiple = _volume_multiple(text) or 1.5
        if _volume_baseline_period(text) is None:
            defaulted.append(_default_path(action, "volume_price", "baseline_period"))
        if _price_move_threshold(text) is None:
            defaulted.append(_default_path(action, "volume_price", "return_threshold_pct"))
        if _volume_multiple(text) is None:
            defaulted.append(_default_path(action, "volume_price", "volume_multiple"))
        intents.append(
            _intent(
                "volume.price_confirmation",
                "surge_down" if re.search(r"(?:齐跌|大跌|下跌)", text) else "surge_up",
                params=(
                    ("baseline_period", baseline_period),
                    ("return_threshold_pct", return_threshold),
                    ("volume_multiple", volume_multiple),
                ),
            )
        )
    elif "relative_volume" in clause_families:
        multiple_match = re.search(rf"(?:放量|缩量|成交量)[^，。；;]*?{_NUMBER}\s*倍", text)
        if (
            "放量" not in text
            and "缩量" not in text
            and multiple_match is None
            and re.search(r"成交量[^，。；;]*(?:达到|超过|高于|大于|低于|少于|小于)", text) is None
        ):
            return [], [], "ambiguous_volume_direction"
        is_at_least = re.search(r"(?:不低于|不小于|不少于)", text) is not None
        is_at_most = re.search(r"(?:不高于|不大于|不超过)", text) is not None
        is_low_volume = is_at_most or (
            not is_at_least
            and (
                "缩量" in text
                or re.search(r"成交量[^，。；;]*(?:低于|少于|小于)", text) is not None
            )
        )
        is_consecutive = re.search(r"(?:连续|持续)[^，。；;]{0,6}放量", text) is not None
        multiple = (
            float(multiple_match.group(1)) if multiple_match else (0.7 if is_low_volume else 1.5)
        )
        if is_consecutive and multiple_match is None:
            multiple = 1.2
        if multiple_match is None:
            defaulted.append(_default_path(action, "volume", "value"))
        intents.append(
            _intent(
                "volume.relative",
                (
                    "consecutive_gte_multiple"
                    if is_consecutive
                    else "lte_multiple"
                    if is_low_volume
                    else "gte_multiple"
                ),
                params=(
                    ("baseline_period", _volume_baseline_period(text) or 20),
                    ("consecutive_days", _consecutive_volume_days(text) or 3),
                ),
                value=multiple,
            )
        )

    return intents, defaulted, None


def _intent(
    indicator_id: str,
    trigger: str,
    *,
    params: tuple[tuple[str, str | int | float | bool], ...],
    value: float | None = None,
) -> IndicatorIntent:
    return IndicatorIntent(
        indicator_id=indicator_id,
        definition_version="1.0.0",
        trigger=trigger,
        params=tuple(sorted(params)),
        value=value,
    )


def _unsupported(symbol: str | None, code: str, confidence: float) -> CandidateAst:
    return CandidateAst(
        instrument_symbol=symbol,
        entry=(),
        exit=(),
        confidence=confidence,
        unsupported_code=code,
    )


def _action_clauses(text: str) -> tuple[_Clause, ...]:
    prefixed = _prefixed_action_clauses(text)
    if prefixed:
        return prefixed
    clauses: list[_Clause] = []
    cursor = 0
    for match in _ACTION_RE.finditer(text):
        body = _clean_clause(text[cursor : match.start()])
        cursor = match.end()
        if not body:
            continue
        action: _Action = "entry" if match.group().startswith("买") else "exit"
        clauses.append(_Clause(text=body, action=action))
    return tuple(clauses)


def _prefixed_action_clauses(text: str) -> tuple[_Clause, ...]:
    marker_re = re.compile(r"(买入|卖出)条件(?:是|为|：|:)?")
    markers = tuple(marker_re.finditer(text))
    if not markers:
        return ()
    clauses: list[_Clause] = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        body = _clean_clause(text[marker.end() : end])
        if body:
            clauses.append(
                _Clause(text=body, action="entry" if marker.group(1) == "买入" else "exit")
            )
    return tuple(clauses)


def _clean_clause(value: str) -> str:
    return re.sub(r"^[，,。；;、]*(?:然后|再|则|就)?", "", value).strip("，,。；;、")


def _explicit_families(text: str) -> frozenset[_Family]:
    folded = text.casefold()
    families: set[_Family] = set()
    if "macd" in folded or "dif" in folded or "dea" in folded:
        families.add("macd")
    if "rsi" in folded or "相对强弱" in text:
        families.add("rsi")
    is_stochastic = (
        re.search(
            r"(?<![a-z])(?:stochastic|stoch)"
            r"(?=$|[^a-z]|[kd](?:线|值)?(?:上穿|下穿|突破|跌破|高于|低于|大于|小于))",
            folded,
        )
        is not None
        or "随机振荡指标" in text
    )
    if is_stochastic:
        families.add("stochastic")
    elif "kdj" in folded or "随机指标" in text or _kdj_line_cross_trigger(text) is not None:
        families.add("kdj")
    if "cci" in folded or "顺势指标" in text:
        families.add("cci")
    if "bbi" in folded or "多空指标" in text:
        families.add("bbi")
    if "布林" in text or "boll" in folded:
        families.add("bollinger")
    if _contains_ema_bias(text):
        families.add("ema_bias")
    elif "ema" in folded or "指数移动平均" in text or "指数均线" in text:
        families.add("ema")
    if _ma_pair_relation(text) is not None or re.search(r"均线[^，。；;]*(?:金叉|死叉)", text):
        families.add("ma_cross")
    elif re.search(r"\d{1,4}日(?:均线|线|ma)", text, re.IGNORECASE):
        families.add("ma")
    if re.search(
        r"(?:收益率标准差|收益标准差|(?<![a-z])returnstd(?![a-z]))",
        folded,
    ):
        families.add("return_stddev")
    elif re.search(r"(?:涨跌幅|涨幅|跌幅|收益率)", text):
        families.add("return_pct")
    if re.search(
        r"(?:历史波动率|年化波动率|(?<![a-z])(?:historicalvolatility|histvol)(?![a-z]))",
        folded,
    ):
        families.add("historical_volatility")
    if re.search(r"(?<![a-z])natr(?![a-z])", folded) or "归一化atr" in folded:
        families.add("natr")
    elif re.search(r"(?<![a-z])atr(?![a-z])", folded) or "平均真实波幅" in text:
        families.add("atr")
    elif "真实波幅" in text or re.search(r"(?<![a-z])tr(?![a-z])", folded):
        families.add("true_range")
    if re.search(r"(?<![a-z])adx(?![a-z])", folded) or "平均趋向指数" in text:
        families.add("adx")
    if (
        re.search(r"(?<![a-z])dmi(?![a-z])", folded)
        or "趋向指标" in text
        or re.search(r"[+-]di", folded)
    ):
        families.add("dmi")
    if (
        re.search(r"(?<![a-z])bias(?![a-z])", folded) or "乖离率" in text
    ) and "ema_bias" not in families:
        families.add("bias")
    if re.search(r"(?<![a-z])roc(?![a-z])", folded) or "变动率" in text:
        families.add("roc")
    if (
        re.search(r"(?<![a-z])mom(?![a-z])", folded)
        or re.search(r"(?<![a-z])momentum(?![a-z])", folded)
        or "动量指标" in text
    ):
        families.add("momentum")
    if re.search(
        r"(?:(?<![a-z])williams(?:%r)?(?![a-z])|威廉指标|威廉%r|"
        r"(?<![a-z])wr(?:指标)?(?![a-z]))",
        folded,
    ):
        families.add("williams_r")
    if re.search(r"(?<![a-z])donchian(?![a-z])", folded) or "唐奇安" in text:
        families.add("donchian")
    if _is_rolling_high_request(text):
        families.add("rolling_high")
    if re.search(
        r"(?:连续上涨|连涨)(?:(?:不超过|至少|超过|大于|多于|少于|小于|不满|至多))?"
        r"\d{0,4}(?:天|日)?",
        text,
    ):
        families.add("consecutive_up")
    if "振幅" in text:
        families.add("amplitude")
    if "平均成交额" in text or "日均成交额" in text:
        families.add("amount_average")
    elif "成交额" in text:
        families.add("amount")
    if re.search(r"(?:量价|obv|能量潮)[^，。；;]*(?:顶背离|底背离)", text, re.I):
        families.add("volume_divergence")
    elif "obv" in folded or "能量潮" in text:
        families.add("obv")
    if re.search(
        r"(?:(?:阶段|当前)?(?:上涨|上升|下跌|下降|震荡|横盘|盘整|多头|空头)趋势|趋势(?:向上|向下|转强|转弱))",
        text,
    ):
        families.add("trend_regime")
    if re.search(r"(?:放量(?:大涨|大跌|上涨|下跌)|量价齐(?:升|跌))", text):
        families.add("volume_price")
    elif "放量" in text or "缩量" in text or "成交量" in text:
        families.add("relative_volume")
    return frozenset(families)


def _contains_ema_bias(text: str) -> bool:
    return (
        re.search(r"(?:ema\s*\d*|指数(?:移动)?均线)[^，。；;]{0,8}乖离率?", text, re.I) is not None
    )


def _technical_negation_error(
    text: str,
    clause_families: frozenset[_Family],
    global_families: frozenset[_Family],
) -> str | None:
    families = clause_families or global_families
    if not families:
        return None
    if re.search(r"(?:不是|不要|别在|禁止|避免|尚未|未曾|没有)(?:[^，。；;]{0,16})", text):
        return "negated_signal_not_supported"
    inclusive = re.search(r"(?:不低于|不小于|不少于|不高于|不大于|不超过)", text)
    if inclusive is not None:
        if families == frozenset({"relative_volume"}) and re.search(
            r"(?:成交量|放量|缩量)[^，。；;]*"
            r"(?:不低于|不小于|不少于|不高于|不大于|不超过)",
            text,
        ):
            return None
        return "inclusive_comparator_not_supported"
    if re.search(
        r"不(?:上涨|下跌|上升|下降|上行|下行|放量|缩量|突破|跌破|金叉|死叉|高于|低于)",
        text,
    ):
        return "negated_signal_not_supported"
    return None


def _has_explicit_call(text: str, family: str) -> bool:
    return re.search(rf"{re.escape(family)}[（(]", text, re.I) is not None


def _macd_params(text: str) -> tuple[int, int, int] | None:
    match = re.search(
        r"macd(?:[（(])?(\d{1,4})[,，/](\d{1,4})[,，/](\d{1,4})(?:[）)])?",
        text,
        re.I,
    )
    if match is None:
        return None
    return tuple(int(match.group(index)) for index in range(1, 4))  # type: ignore[return-value]


def _rsi_period(text: str) -> int | None:
    match = re.search(r"rsi(?:[（(](\d{1,4})[）)]|(\d{1,4}))", text, re.I)
    if match is None:
        return None
    return int(match.group(1) or match.group(2))


def _kdj_params(text: str) -> tuple[int, int, int] | None:
    full = re.search(
        r"kdj[（(](\d{1,4})[,，/](\d{1,4})[,，/](\d{1,4})[）)]",
        text,
        re.I,
    )
    if full is not None:
        return tuple(int(full.group(index)) for index in range(1, 4))  # type: ignore[return-value]
    period = re.search(r"kdj(\d{1,4})", text, re.I)
    if period is None:
        return None
    return int(period.group(1)), 3, 3


def _is_rolling_high_request(text: str) -> bool:
    if re.search(
        r"(?:低于|小于|下穿|跌破|未创|没有)[^，。；;]{0,12}\d{1,4}日(?:新高|最高价)", text
    ):
        return False
    return (
        re.search(
            r"(?:(?:创|刷新|突破|超过|高于)[^，。；;]{0,4})?\d{1,4}日(?:新高|最高价)",
            text,
        )
        is not None
    )


def _has_unmodeled_direction(text: str) -> bool:
    return (
        re.search(
            r"(?:上涨|下跌|上升|下降|上行|下行|向上|向下|走强|走弱|转强|转弱)",
            text,
        )
        is not None
    )


def _resolve_cross_family(
    text: str,
    clause_families: frozenset[_Family],
    global_families: frozenset[_Family],
) -> tuple[_Family | None, str | None]:
    if "金叉" not in text and "死叉" not in text:
        return None, None
    local = clause_families & _CROSS_FAMILIES
    if len(local) == 1:
        return next(iter(local)), None
    if len(local) > 1 or clause_families:
        return None, "ambiguous_cross_indicator"
    inherited = global_families & _CROSS_FAMILIES
    if len(inherited) == 1:
        return next(iter(inherited)), None
    return None, "ambiguous_cross_indicator"


def _family_active(
    family: _Family,
    text: str,
    clause_families: frozenset[_Family],
    global_families: frozenset[_Family],
) -> bool:
    if family in clause_families:
        return True
    if clause_families or global_families != frozenset({family}):
        return False
    if family in {
        "adx",
        "atr",
        "bias",
        "rsi",
        "cci",
        "ema_bias",
        "return_pct",
        "amplitude",
        "amount",
        "amount_average",
        "historical_volatility",
        "momentum",
        "natr",
        "return_stddev",
        "roc",
        "true_range",
        "williams_r",
    }:
        return _threshold_rule(text) is not None or any(
            word in text for word in ("超买", "超卖", "乖离")
        )
    if family == "bollinger":
        return any(rail in text for rail in ("上轨", "中轨", "下轨"))
    return _has_price_operator(text)


def _inherits_j_rule(
    text: str,
    clause_families: frozenset[_Family],
    global_families: frozenset[_Family],
) -> bool:
    return (
        not clause_families
        and global_families == frozenset({"kdj"})
        and (_mentions_kdj_j_value(text) or re.search(r"(?:低位|高位)", text) is not None)
    )


def _mentions_kdj_j_value(text: str) -> bool:
    return (
        re.search(
            r"(?:j值|j线|(?<!kd)j(?=(?:上穿|下穿|突破|跌破|高于|低于|大于|小于|超过)))",
            text,
            re.I,
        )
        is not None
    )


def _macd_trigger(text: str, action: _Action) -> str | None:
    """Resolve explicit DIF/DEA and zero-axis language before applying defaults."""

    if "金叉" in text:
        return "golden_cross"
    if "死叉" in text:
        return "death_cross"

    line_cross = re.search(
        r"(?P<left>dif|dea)(?:线|值)?(?P<op>上穿|下穿|突破|跌破)"
        r"(?P<right>dif|dea)(?:线|值)?",
        text,
        re.I,
    )
    if line_cross is not None:
        left = line_cross.group("left").casefold()
        right = line_cross.group("right").casefold()
        if left == right:
            return None
        left_crosses_above = line_cross.group("op") in {"上穿", "突破"}
        dif_crosses_above = left_crosses_above if left == "dif" else not left_crosses_above
        return "golden_cross" if dif_crosses_above else "death_cross"

    zero_cross = re.search(
        r"(?:macd|dif)?(?:线|值)?(?P<op>上穿|下穿|突破|跌破|站上|失守)(?:零|0)轴",
        text,
        re.I,
    )
    if zero_cross is not None:
        return (
            "crosses_above_zero"
            if zero_cross.group("op") in {"上穿", "突破", "站上"}
            else "crosses_below_zero"
        )

    if _has_unmodeled_direction(text) or re.search(
        r"(?:(?:零|0)轴|dif|dea|上穿|下穿|突破|跌破)", text, re.I
    ):
        return None
    return "golden_cross" if action == "entry" else "death_cross"


def _kdj_line_cross_trigger(text: str) -> str | None:
    match = re.search(
        r"(?P<left>[kd])(?:线|值)?(?P<op>上穿|下穿|突破|跌破)"
        r"(?P<right>[kd])(?:线|值)?",
        text,
        re.I,
    )
    if match is None:
        return None
    left = match.group("left").casefold()
    right = match.group("right").casefold()
    if left == right:
        return None
    left_crosses_above = match.group("op") in {"上穿", "突破"}
    k_crosses_above = left_crosses_above if left == "k" else not left_crosses_above
    return "golden_cross" if k_crosses_above else "death_cross"


def _mentions_incomplete_kdj_cross(text: str) -> bool:
    return re.search(r"(?:[kd](?:线|值)?).*(?:上穿|下穿|突破|跌破)", text, re.I) is not None


def _kdj_cross_trigger(text: str, action: _Action) -> str | None:
    if "金叉" in text:
        return "golden_cross"
    if "死叉" in text:
        return "death_cross"
    explicit = _kdj_line_cross_trigger(text)
    if explicit is not None:
        return explicit
    if _mentions_incomplete_kdj_cross(text):
        return None
    return "golden_cross" if action == "entry" else "death_cross"


def _ma_pair_relation(text: str) -> tuple[int, int, str] | None:
    match = re.search(
        r"(?P<left>\d{1,4})日(?:均线|线|ma|sma)"
        r"(?P<op>上穿|下穿|突破|跌破|高于|低于)"
        r"(?P<right>\d{1,4})日(?:均线|线|ma|sma)",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return None
    left, right = int(match.group("left")), int(match.group("right"))
    if left == right:
        return None
    is_cross = match.group("op") in {"上穿", "下穿", "突破", "跌破"}
    left_above = match.group("op") in {"上穿", "突破", "高于"}
    fast_above = left_above if left < right else not left_above
    if is_cross:
        trigger = "golden_cross" if fast_above else "death_cross"
    else:
        trigger = "fast_above_slow" if fast_above else "fast_below_slow"
    return min(left, right), max(left, right), trigger


def _ma_cross_trigger(text: str, action: _Action) -> str:
    if "金叉" in text:
        return "golden_cross"
    if "死叉" in text:
        return "death_cross"
    relation = _ma_pair_relation(text)
    if relation is None:
        return "golden_cross" if action == "entry" else "death_cross"
    return relation[2]


def _price_trigger(text: str, action: _Action) -> str:
    if re.search(r"(?:上穿|突破|站上)", text):
        return "price_crosses_above"
    if re.search(r"(?:下穿|跌破|失守)", text):
        return "price_crosses_below"
    if re.search(r"(?:高于|大于|(?:在|处于|位于)?.{0,12}上方)", text):
        return "price_above"
    if re.search(r"(?:低于|小于|(?:在|处于|位于)?.{0,12}下方)", text):
        return "price_below"
    return "price_crosses_above" if action == "entry" else "price_crosses_below"


def _has_price_operator(text: str) -> bool:
    return re.search(r"(?:上穿|突破|站上|下穿|跌破|失守|高于|低于|上方|下方)", text) is not None


def _has_unsupported_numeric_relation(text: str) -> bool:
    if re.search(r"(?:至少|至多|等于|达到|介于|不多于|不少于|不大于|不小于)", text):
        return re.search(_SIGNED_NUMBER, text) is not None
    if re.search(r"(?:>=|<=|≥|≤|==|=)", text):
        return re.search(_SIGNED_NUMBER, text) is not None
    return (
        re.search(rf"{_SIGNED_NUMBER}[^，。；;]*到[^，。；;]*{_SIGNED_NUMBER}[^，。；;]*之间", text)
        is not None
    )


def _threshold_rule(text: str) -> tuple[float, str] | None:
    definitions = (
        (("上穿", "突破"), "crosses_above"),
        (("下穿", "跌破"), "crosses_below"),
        (("高于", "大于", "超过", ">"), "above"),
        (("低于", "小于", "少于", "不足", "<"), "below"),
    )
    for operators, trigger in definitions:
        operator_pattern = "|".join(re.escape(item) for item in operators)
        match = re.search(rf"(?:{operator_pattern})[^\d+-]*{_SIGNED_NUMBER}", text, re.I)
        if match is not None:
            return float(match.group(1)), trigger
    return None


def _return_threshold_rule(text: str) -> tuple[float, str] | None:
    rule = _threshold_rule(text)
    if rule is None or re.search(r"(?<!涨)跌幅", text) is None:
        return rule
    value, trigger = rule
    mirrored_trigger = {
        "above": "below",
        "below": "above",
        "crosses_above": "crosses_below",
        "crosses_below": "crosses_above",
    }[trigger]
    return -abs(value), mirrored_trigger


def _amount_threshold_rule(text: str) -> tuple[float, str] | None:
    definitions = (
        (("上穿", "突破"), "crosses_above"),
        (("下穿", "跌破"), "crosses_below"),
        (("高于", "大于", "超过", ">"), "above"),
        (("低于", "小于", "少于", "不足", "<"), "below"),
    )
    multipliers = {
        None: 1.0,
        "元": 1.0,
        "万": 10_000.0,
        "万元": 10_000.0,
        "亿": 100_000_000.0,
        "亿元": 100_000_000.0,
    }
    for operators, trigger in definitions:
        operator_pattern = "|".join(re.escape(item) for item in operators)
        match = re.search(
            rf"(?:{operator_pattern})[^\d+-]*{_SIGNED_NUMBER}\s*(亿元|万元|亿|万|元)?",
            text,
            re.I,
        )
        if match is not None:
            return float(match.group(1)) * multipliers[match.group(2)], trigger
    return None


def _return_period(text: str) -> int | None:
    match = re.search(r"(\d{1,4})日(?:涨跌幅|涨幅|跌幅|收益率)", text)
    return int(match.group(1)) if match is not None else None


def _rolling_high_period(text: str) -> int | None:
    match = re.search(r"(?:创|突破)?(\d{1,4})日(?:新高|最高价)", text)
    return int(match.group(1)) if match is not None else None


def _mentions_consecutive_up_rule(text: str) -> bool:
    return (
        re.search(
            r"(?:(?:连续上涨|连涨)"
            r"(?:不超过|至少|超过|大于|多于|少于|小于|不满|至多)?"
            r"\d{1,4}(?:天|日)|"
            r"(?:不超过|至少|超过|大于|多于|少于|小于|不满|至多)"
            r"\d{1,4}(?:天|日))",
            text,
        )
        is not None
    )


def _consecutive_up_rule(text: str) -> tuple[int | None, str | None]:
    match = re.search(
        r"(?:(?:连续上涨|连涨))?"
        r"(?P<operator>不超过|至少|超过|大于|多于|少于|小于|不满|至多)?"
        r"(?P<days>\d{1,4})(?:天|日)",
        text,
    )
    if match is None:
        return None, None
    operator = match.group("operator")
    if operator in {"不超过", "少于", "小于", "不满", "至多"}:
        return None, "unsupported_consecutive_up_comparator"
    days = int(match.group("days"))
    if operator in {"超过", "大于", "多于"}:
        days += 1
    return days, None


def _amount_average_period(text: str) -> int | None:
    match = re.search(r"(\d{1,4})日(?:平均成交额|日均成交额)", text)
    return int(match.group(1)) if match is not None else None


def _volume_baseline_period(text: str) -> int | None:
    match = re.search(r"(?:过去|此前|前)?(\d{1,4})日(?:平均)?(?:成交)?量", text)
    return int(match.group(1)) if match is not None else None


def _volume_multiple(text: str) -> float | None:
    match = re.search(rf"(?:放量|成交量)[^，。；;]*?{_NUMBER}\s*倍", text)
    return float(match.group(1)) if match is not None else None


def _price_move_threshold(text: str) -> float | None:
    match = re.search(rf"(?:大涨|大跌|上涨|下跌|涨幅|跌幅)[^，。；;]*?{_NUMBER}\s*%", text)
    return float(match.group(1)) if match is not None else None


def _consecutive_volume_days(text: str) -> int | None:
    patterns = (
        r"(?:连续|持续)(\d{1,3})(?:天|日)?[^，。；;]{0,4}放量",
        r"(?:连续|持续)[^，。；;]{0,4}放量(\d{1,3})(?:天|日)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match is not None:
            return int(match.group(1))
    return None


def _ema_period(text: str) -> int | None:
    patterns = (
        r"ema[（(]?(\d{1,4})",
        r"(\d{1,4})日(?:ema|指数(?:移动)?均线)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match is not None:
            return int(match.group(1))
    return None


def _ma_price_period(text: str) -> int | None:
    patterns = (
        r"(?:股价|价格|收盘价)?(?:上穿|突破|站上|下穿|跌破|失守|高于|低于)(\d{1,4})日(?:均线|线|ma)",
        r"(\d{1,4})日(?:均线|线|ma)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match is not None:
            return int(match.group(1))
    return None


def _bollinger_period(text: str) -> int | None:
    match = re.search(r"(?:(\d{1,4})日)?(?:布林(?:带)?|boll(?:inger)?)", text, re.I)
    return int(match.group(1)) if match is not None and match.group(1) else None


def _bollinger_trigger(text: str) -> str | None:
    rail = next((name for name in ("上轨", "中轨", "下轨") if name in text), None)
    if rail is None:
        return None
    rail_id = {"上轨": "upper", "中轨": "middle", "下轨": "lower"}[rail]
    if re.search(r"(?:上穿|突破|站上)", text):
        return f"price_crosses_above_{rail_id}"
    if re.search(r"(?:下穿|跌破|失守)", text):
        return f"price_crosses_below_{rail_id}"
    return None


def _cci_period(text: str) -> int | None:
    match = re.search(r"(?:cci[（(]?(\d{1,4})|(\d{1,4})日cci)", text, re.I)
    if match is None:
        return None
    return int(match.group(1) or match.group(2))


def _bbi_periods(text: str) -> tuple[int, int, int, int] | None:
    match = re.search(
        r"bbi[（(](\d{1,4})[,，/](\d{1,4})[,，/](\d{1,4})[,，/](\d{1,4})[）)]",
        text,
        re.I,
    )
    if match is None:
        return None
    return tuple(int(match.group(index)) for index in range(1, 5))  # type: ignore[return-value]


def _p1_period(
    text: str,
    full_text: str,
    family: _Family,
    default: int,
) -> int:
    aliases: dict[_Family, str] = {
        "atr": r"(?<![a-z])atr(?![a-z])|平均真实波幅",
        "natr": r"(?<![a-z])natr(?![a-z])|归一化atr",
        "adx": r"(?<![a-z])adx(?![a-z])|平均趋向指数",
        "dmi": r"(?<![a-z])dmi(?![a-z])|趋向指标|[+-]di",
        "bias": r"(?<![a-z])bias(?![a-z])|(?<!ema)乖离率",
        "roc": r"(?<![a-z])roc(?![a-z])|变动率",
        "momentum": r"(?<![a-z])(?:mom|momentum)(?![a-z])|动量指标",
        "williams_r": (
            r"(?<![a-z])williams(?:%r)?(?![a-z])|威廉(?:指标|%r)|"
            r"(?<![a-z])wr(?:指标)?(?![a-z])"
        ),
        "return_stddev": r"收益率?标准差|(?<![a-z])returnstd(?![a-z])",
        "historical_volatility": (
            r"历史波动率|年化波动率|(?<![a-z])(?:historicalvolatility|histvol)(?![a-z])"
        ),
        "donchian": r"(?<![a-z])donchian(?![a-z])|唐奇安(?:通道)?",
    }
    alias = aliases.get(family)
    if alias is None:
        return default
    for candidate in (text, full_text):
        suffix = re.search(rf"(?:{alias})[（(]?(\d{{1,4}})[）)]?", candidate, re.I)
        if suffix is not None:
            return int(suffix.group(1))
        prefix = re.search(rf"(\d{{1,4}})日(?:{alias})", candidate, re.I)
        if prefix is not None:
            return int(prefix.group(1))
    return default


def _p1_stochastic_period(text: str, full_text: str, line: str, default: int) -> int:
    for candidate in (text, full_text):
        full = re.search(
            r"(?:stochastic|stoch|随机振荡指标)[（(](\d{1,4})[,，/](\d{1,4})[）)]",
            candidate,
            re.I,
        )
        if full is not None:
            return int(full.group(1 if line == "k" else 2))
        named = re.search(rf"{line}(?:周期|period)?[=:：]?(\d{{1,4}})", candidate, re.I)
        if named is not None:
            return int(named.group(1))
    return default


def _dmi_trigger(text: str) -> str | None:
    match = re.search(
        r"(?P<left>\+di|-di|pdi|mdi)(?P<op>上穿|下穿|突破|跌破|高于|低于|大于|小于)"
        r"(?P<right>\+di|-di|pdi|mdi)",
        text,
        re.I,
    )
    if match is None:
        return None
    left = match.group("left").casefold()
    right = match.group("right").casefold()
    plus_aliases = {"+di", "pdi"}
    minus_aliases = {"-di", "mdi"}
    if not (
        (left in plus_aliases and right in minus_aliases)
        or (left in minus_aliases and right in plus_aliases)
    ):
        return None
    op = match.group("op")
    left_above = op in {"上穿", "突破", "高于", "大于"}
    plus_above = left_above if left in plus_aliases else not left_above
    is_cross = op in {"上穿", "下穿", "突破", "跌破"}
    if is_cross:
        return "plus_crosses_above_minus" if plus_above else "plus_crosses_below_minus"
    return "plus_above_minus" if plus_above else "plus_below_minus"


def _stochastic_rule(text: str) -> tuple[str, float | None] | None:
    line_cross = re.search(
        r"(?P<left>[kd])(?:线|值)?(?P<op>上穿|下穿|突破|跌破)"
        r"(?P<right>[kd])(?:线|值)?",
        text,
        re.I,
    )
    if line_cross is not None:
        left = line_cross.group("left").casefold()
        right = line_cross.group("right").casefold()
        if left == right:
            return None
        left_above = line_cross.group("op") in {"上穿", "突破"}
        k_above = left_above if left == "k" else not left_above
        return ("k_crosses_above_d" if k_above else "k_crosses_below_d", None)
    if re.search(r"k(?:线|值)?", text, re.I) is None:
        return None
    threshold = _threshold_rule(text)
    if threshold is None:
        return None
    value, trigger = threshold
    if trigger not in {"above", "below"}:
        return None
    return ("k_above" if trigger == "above" else "k_below", value)


def _donchian_trigger(text: str) -> str | None:
    rail = "upper" if "上轨" in text else "lower" if "下轨" in text else None
    if rail is None:
        return None
    if re.search(r"(?:上穿|突破|站上)", text) and rail == "upper":
        return "price_crosses_above_upper"
    if re.search(r"(?:下穿|跌破|失守)", text) and rail == "lower":
        return "price_crosses_below_lower"
    if re.search(r"(?:高于|大于|上方)", text) and rail == "upper":
        return "price_above_upper"
    if re.search(r"(?:低于|小于|下方)", text) and rail == "lower":
        return "price_below_lower"
    return None


def _default_path(action: _Action, family: str, field: str) -> str:
    prefix = "/entry" if action == "entry" else "/exit"
    return f"{prefix}/{family}/{field}"


def _event_intent(text: str) -> EventIntent | None:
    folded = text.casefold()
    if (
        _NEGATED_EVENT_STAGE_RE.search(text) is not None
        or _DENIED_OR_CLARIFICATION_EVENT_RE.search(text) is not None
    ):
        return None
    is_web_event_context = any(word in folded for word in _WEB_EVENT_CONTEXT_WORDS)
    if is_web_event_context and any(phrase in folded for phrase in _NON_FINAL_WEB_EVENT_PHRASES):
        return None
    if any(phrase in text for phrase in _FINAL_AWARD_PHRASES):
        return EventIntent(
            event_code="event.contracts_orders.major_contract_won",
            definition_version="1.0.0",
        )
    if _LICENSE_APPROVAL_RE.search(text) is not None:
        return EventIntent(
            event_code="event.macro_policy_industry.license_approval",
            definition_version="1.0.0",
        )
    for pattern, event_code in _ANNOUNCEMENT_EVENT_DEFINITIONS:
        if pattern.search(text) is not None:
            return EventIntent(event_code=event_code, definition_version="1.0.0")
    definitions = (
        ("业绩预告", "event.financial_results.earnings_forecast_published"),
        ("业绩快报", "event.financial_results.earnings_flash_report"),
        # Match the more specific half-year phrases before ``年度报告`` / ``年报``.
        # Both shorter annual-report phrases are substrings of the Chinese
        # half-year names, so reversing this order silently changes the event.
        ("半年度报告", "event.financial_results.semiannual_report"),
        ("半年报", "event.financial_results.semiannual_report"),
        ("中报", "event.financial_results.semiannual_report"),
        ("季度报告", "event.financial_results.quarterly_report"),
        ("季报", "event.financial_results.quarterly_report"),
        ("年度报告", "event.financial_results.annual_report"),
        ("年报", "event.financial_results.annual_report"),
    )
    for phrase, event_code in definitions:
        if phrase in text:
            return EventIntent(
                event_code=event_code,
                definition_version="1.0.0",
                document_text=_document_text_intent(text),
            )
    return None


def _document_text_intent(text: str) -> DocumentTextIntent | None:
    for pattern in _DOCUMENT_TERM_COUNT_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        term = match.group("term")
        comparator = _document_comparator(match.group("comparator"))
        value = int(match.group("value"))
        return DocumentTextIntent(
            term=term,
            match_mode=(
                "ascii_token"
                if term.isascii() and any(character.isalnum() for character in term)
                else "literal"
            ),
            comparator=comparator,
            value=value,
            case_sensitive=False,
        )
    return None


def _document_comparator(value: str) -> Literal["gt", "gte"]:
    if value in {"超过", "大于", "多于", ">"}:
        return "gt"
    return "gte"


def _holding_period_intent(text: str) -> tuple[int, bool] | None:
    match = _PREFIXED_HOLDING_PERIOD_RE.search(text) or _BARE_HOLDING_PERIOD_RE.fullmatch(text)
    if match is None or "内" in text:
        return None
    sessions = int(match.group("sessions"))
    if not 1 <= sessions <= 10_000:
        return None
    explicit_trading_unit = match.group("unit") in {"交易日", "交易天"}
    return sessions, not explicit_trading_unit


def _explicit_risk_exit_intents(
    text: str,
    action: _Action,
) -> tuple[list[ExitIntent], str | None]:
    hints = {
        "take_profit": "止盈" in text,
        "stop_loss": "止损" in text,
        "trailing_drawdown": "回撤" in text,
    }
    if not any(hints.values()):
        return [], None
    if action != "exit":
        return [], "position_risk_rule_only_supported_for_exit"

    intents: list[ExitIntent] = []
    definitions: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("take_profit", re.compile(r"止盈(?P<value>\d+(?:\.\d+)?)%")),
        ("stop_loss", re.compile(r"止损(?P<value>\d+(?:\.\d+)?)%")),
        (
            "trailing_drawdown",
            re.compile(
                r"(?:(?:从|较)(?:持仓)?(?:最高点|高点)|(?:持仓)?最高点)?"
                r"回撤(?P<value>\d+(?:\.\d+)?)%"
            ),
        ),
    )
    for kind, pattern in definitions:
        if not hints[kind]:
            continue
        values = {float(match.group("value")) for match in pattern.finditer(text)}
        if not values:
            return [], f"{kind}_threshold_required"
        if len(values) != 1:
            return [], f"ambiguous_{kind}_threshold"
        threshold = values.pop()
        if kind == "trailing_drawdown":
            intents.append(TrailingDrawdownIntent(threshold_pct=threshold))
        else:
            trigger = "take_profit" if kind == "take_profit" else "stop_loss"
            intents.append(
                PositionReturnIntent(
                    trigger=trigger,
                    threshold_pct=threshold,
                )
            )
    return intents, None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).strip()


def _normalize_context(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return normalize_a_share_instrument(value).value
    except AshareInstrumentCodeError:
        return None


def _extract_symbol(text: str) -> str | None:
    match = _SYMBOL_RE.search(text)
    if match is None:
        return None
    code, explicit_exchange = match.groups()
    raw = f"{code}.{explicit_exchange.upper()}" if explicit_exchange else code
    try:
        return normalize_a_share_instrument(raw).value
    except AshareInstrumentCodeError:
        return None
