"""Eastmoney company-announcement observations with point-in-time safeguards.

Eastmoney quote data lives on ``push2``.  Company announcements do not: the
web product reads the separate ``np-anotice-stock`` service.  This adapter
keeps that boundary explicit and treats ``eiTime`` as a vendor observation,
never as an exchange-certified publication timestamp.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import cast
from urllib.parse import unquote, urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx

from ashare_lab.adapters.host_http import HostThrottle, HostThrottledHttpClient
from ashare_lab.domain.events.catalog import DOCUMENT_TEXT_EVENT_CODES
from ashare_lab.domain.events.observations import EventObservation
from ashare_lab.domain.market_data import TimeQuality
from ashare_lab.domain.shared import InstrumentId

from .announcement_acceptance import TITLE_RULE_ACCEPTANCE_CASES
from .collector import EventFetchBatch
from .document_text import (
    AnnouncementDocumentTextExtractor,
    DocumentTextExtractionError,
    ExtractedDocumentText,
)

ANNOUNCEMENT_LIST_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
ANNOUNCEMENT_CONTENT_URL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
ANNOUNCEMENT_PDF_TEMPLATE = "https://pdf.dfcfw.com/pdf/H2_{art_code}_1.pdf"
COMPLETE_PROVIDER_COLUMN_COVERAGE_BASIS = (
    "complete_announcement_interval_with_provider_column_classification"
)
COMPLETE_DETERMINISTIC_TITLE_COVERAGE_BASIS = (
    "complete_announcement_interval_with_deterministic_title_classification"
)
ANNOUNCEMENT_CLASSIFIER_VERSION = "eastmoney-announcement-classifier.v2"

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DATE_ONLY_CLOSE = time(hour=15)
_MIN_CREDIBLE_EITIME_YEAR = 2017
_MAX_TIMESTAMP_DISTANCE_DAYS = 7
_MAX_PROVIDER_PAGE_SIZE = 100
_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_MAX_PAGES = 100
_MIN_REQUEST_INTERVAL_SECONDS = 1.0
_ART_CODE = re.compile(r"^AN[0-9]{12,32}$")
_PROVIDER_DATETIME = re.compile(
    r"^(?P<seconds>[0-9]{4}-[0-9]{2}-[0-9]{2} "
    r"[0-9]{2}:[0-9]{2}:[0-9]{2})(?::(?P<milliseconds>[0-9]{3}))?$"
)
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://data.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (compatible; AShareLab/1.0; announcement-backfill)",
}

_PDF_HOST = "pdf.dfcfw.com"
_MARKET_CODES_BY_EXCHANGE = {
    "SZ": frozenset({"0"}),
    "SH": frozenset({"1"}),
    # Eastmoney groups Beijing and Shenzhen securities under market marker 0
    # in the announcement product.  The six-digit code remains mandatory.
    "BJ": frozenset({"0"}),
}

_COLUMN_EVENT_CODES = {
    "001002004001": "event.financial_results.earnings_forecast_published",
    "001002004002": "event.financial_results.earnings_flash_report",
    "001002007003009001002": "event.repurchase_capital.repurchase_change",
    "001001001001001": "event.financial_results.annual_report",
    "001001001002001": "event.financial_results.semiannual_report",
    "001001001003001": "event.financial_results.quarterly_report",
    "001001001004001": "event.financial_results.quarterly_report",
}
_PERIODIC_REPORT_TYPES = {
    "event.financial_results.annual_report": "annual_report",
    "event.financial_results.semiannual_report": "semiannual_report",
    "event.financial_results.quarterly_report": "quarterly_report",
    "event.financial_results.earnings_forecast_published": "earnings_forecast",
    "event.financial_results.earnings_flash_report": "earnings_flash",
}
_UNCLASSIFIED_EVENT_CODE = "event.announcement.unclassified"
_AMBIGUOUS_OR_NEGATED_TITLE_RE = re.compile(
    r"(?:传闻不实|不属实|不存在|予以澄清|澄清(?:公告|说明)?|"
    r"网传|网络传闻|网页消息|网页报道|媒体报道|新闻报道|市场消息|"
    r"中标候选人|中标候选|候选中标|预中标|入围|"
    r"未(?:最终|正式)?中标|尚未(?:最终|正式)?中标|中标失败|落标)|"
    r"中标意向|意向中标|拟中标|尚待(?:签署|签订).*(?:合同|协议)|"
    r"尚未(?:签署|签订).*(?:合同|协议)|"
    r"(?:尚未|没有|未|不)(?:能|再)?(?:最终|正式|明确)?"
    r"(?:获得|取得|获批|获|核准|批准|同意注册|审议通过|发布|披露|"
    r"完成|完毕|实施完成|终止|停止|实施|授予|签署|签订|收到|发生|"
    r"受理|被?立案调查|达到|降至|变更|辞职|辞任|下调|注销|除权|除息)"
)
_REVISED_OR_WITHDRAWN_TITLE_RE = re.compile(
    r"(?:更正(?:公告|说明)?|修订(?:稿|版|公告)?|更新版?|补充公告|补充说明|"
    r"撤回|撤销|取消)"
)
_NON_INITIAL_PERIODIC_DOCUMENT_RE = re.compile(
    r"(?:摘要|英文版|外文版|更正|修订|取消|撤回|撤销|更新版?|补充公告|补充说明)"
)
_INITIAL_COMPLETE_ANNUAL_REPORT_RE = re.compile(r"(?:^|:)20\d{2}年?年度报告$")
_INITIAL_COMPLETE_SEMIANNUAL_REPORT_RE = re.compile(r"(?:^|:)20\d{2}年?(?:半年度|半年)报告$")
_INITIAL_COMPLETE_QUARTERLY_REPORT_RE = re.compile(
    r"(?:^|:)20\d{2}年?(?:第?[一1]季度|第?[三3]季度)报告$"
)


def eastmoney_provider_column_codes(event_code: str) -> tuple[str, ...]:
    """Return the fixed provider columns that can prove interval recall.

    An empty tuple means the code relies on the stricter all-announcement-list
    title-classifier contract instead; it does not by itself mean that the
    code is preparable.
    """

    return tuple(
        sorted(code for code, mapped in _COLUMN_EVENT_CODES.items() if mapped == event_code)
    )


@dataclass(frozen=True, slots=True)
class _TitleEventRule:
    rule_id: str
    event_code: str
    pattern: re.Pattern[str]
    excluded: re.Pattern[str] | None = None


@dataclass(frozen=True, slots=True)
class EastmoneyEventCoverageContract:
    """One code's audited classifier and interval-recall contract.

    A Catalog entry alone never creates this contract.  A code is preparable
    only when the complete announcement-list collector can apply either a
    fixed provider column or one or more deterministic title rules to every
    row in the requested stock/date interval.  The shared negation,
    revision/withdrawal and second-level timing gates are part of the contract
    hash, so changing any of them invalidates old acquisition evidence.
    """

    event_code: str
    query_succeeded: bool
    coverage_basis: str
    provider_column_codes: tuple[str, ...]
    title_rule_ids: tuple[str, ...]
    classifier_sha256: str


def _rule(
    rule_id: str,
    event_code: str,
    pattern: str,
    *,
    excluded: str | None = None,
) -> _TitleEventRule:
    return _TitleEventRule(
        rule_id=rule_id,
        event_code=event_code,
        pattern=re.compile(pattern),
        excluded=re.compile(excluded) if excluded is not None else None,
    )


# Ordered from the most specific lifecycle stage to the broader stage.  These
# rules deliberately require explicit Chinese phrases from an issuer title.
# A broad word such as ``回购`` or ``重组`` alone is never enough to classify an
# event.  Provider columns remain preserved as evidence and exact known column
# codes are evaluated before title rules.
_TITLE_EVENT_RULES = (
    # Dividends and corporate actions.
    _rule(
        "cash-dividend-ex-date-v1",
        "event.dividends_corporate_actions.cash_dividend_ex_date",
        r"(?:现金红利|现金股利).*(?:发放日|派发日|除息日)|(?:发放|派发)(?:现金红利|现金股利).*(?:实施|公告)",
    ),
    _rule(
        "cash-dividend-approved-v1",
        "event.dividends_corporate_actions.cash_dividend_approved",
        r"(?:现金分红|派发现金红利).*(?:股东大会审议通过|获批)|(?:股东大会审议通过|获批).*(?:现金分红|派发现金红利)",
    ),
    _rule(
        "cash-dividend-proposal-v1",
        "event.dividends_corporate_actions.cash_dividend_proposal",
        r"(?:现金分红|派发现金红利).*(?:预案|方案)",
        excluded=r"(?:实施|发放日|派发日|除息)",
    ),
    _rule(
        "stock-dividend-ex-date-v1",
        "event.dividends_corporate_actions.stock_dividend_ex_date",
        r"(?:送股|送红股).*(?:实施|除权)",
    ),
    _rule(
        "stock-dividend-proposal-v1",
        "event.dividends_corporate_actions.stock_dividend_proposal",
        r"(?:送股|送红股).*(?:预案|方案)",
        excluded=r"(?:实施|除权)",
    ),
    _rule(
        "capitalization-issue-v1",
        "event.dividends_corporate_actions.capitalization_issue",
        r"资本公积(?:金)?转增(?:股本|股份).*(?:实施|完成)",
    ),
    _rule(
        "rights-issue-v1",
        "event.dividends_corporate_actions.rights_issue",
        r"配股.*(?:发行公告|实施公告|结果公告)",
    ),
    # Restricted shares and ownership changes.
    _rule(
        "unlock-schedule-change-v1",
        "event.restricted_shares_pledges.unlock_schedule_change",
        r"(?:限售股|限售股份|解除限售).*(?:日期|安排).*(?:变更|调整|延期)|(?:变更|调整|延期).*(?:限售股|限售股份).*(?:上市流通|解禁)",
    ),
    _rule(
        "restricted-shares-unlock-v1",
        "event.restricted_shares_pledges.restricted_shares_unlock",
        r"(?:限售股|限售股份|首次公开发行前已发行股份).*(?:解禁|上市流通)|解除限售.*上市流通",
        excluded=r"(?:变更|调整|延期|取消)",
    ),
    _rule(
        "actual-controller-change-v1",
        "event.shareholder_holdings.actual_controller_change",
        r"实际控制人(?:发生)?变更|变更实际控制人",
    ),
    _rule(
        "ownership-below-five-percent-v1",
        "event.shareholder_holdings.ownership_below_five_percent",
        r"持股(?:比例)?.*(?:降至|低于|不足)5%|权益变动.*5%以下",
    ),
    _rule(
        "ownership-reaches-five-percent-v1",
        "event.shareholder_holdings.ownership_reaches_five_percent",
        r"持股(?:比例)?.*(?:达到|增至|超过)5%|权益变动.*达到5%",
    ),
    _rule(
        "executive-decrease-v1",
        "event.shareholder_holdings.executive_decrease",
        r"(?:董监高|董事、监事、高级管理人员|董事|监事|高级管理人员).*(?:减持|卖出)",
        excluded=r"(?:拟减持|减持股份(?:的)?预披露|减持计划(?:预披露|公告)$)",
    ),
    _rule(
        "executive-increase-v1",
        "event.shareholder_holdings.executive_increase",
        r"(?:董监高|董事、监事、高级管理人员|董事|监事|高级管理人员).*(?:增持|买入)",
        excluded=r"(?:拟增持|增持股份(?:的)?预披露|增持计划(?:预披露|公告)$)",
    ),
    _rule(
        "major-holder-decrease-progress-v1",
        "event.shareholder_holdings.major_holder_decrease_progress",
        r"(?:持股5%以上(?:的)?股东|大股东|控股股东|实际控制人).*减持.*(?:进展|完成|完毕|结果)",
    ),
    _rule(
        "major-holder-decrease-plan-v1",
        "event.shareholder_holdings.major_holder_decrease_plan",
        r"(?:持股5%以上(?:的)?股东|大股东|控股股东|实际控制人).*减持.*(?:计划|预披露)",
        excluded=r"(?:进展|完成|完毕|结果|终止)",
    ),
    _rule(
        "major-holder-increase-progress-v1",
        "event.shareholder_holdings.major_holder_increase_progress",
        r"(?:持股5%以上(?:的)?股东|大股东|控股股东|实际控制人).*增持.*(?:进展|完成|完毕|结果)",
    ),
    _rule(
        "major-holder-increase-plan-v1",
        "event.shareholder_holdings.major_holder_increase_plan",
        r"(?:持股5%以上(?:的)?股东|大股东|控股股东|实际控制人).*增持.*(?:计划|拟增持)",
        excluded=r"(?:进展|完成|完毕|结果|终止)",
    ),
    # Repurchase lifecycle.
    _rule(
        "repurchase-termination-v1",
        "event.repurchase_capital.repurchase_termination",
        r"(?:终止|停止).*回购|回购.*(?:终止|停止)",
        excluded=r"不(?:终止|停止)",
    ),
    _rule(
        "repurchase-cancellation-v1",
        "event.repurchase_capital.repurchase_cancellation",
        r"(?:注销|完成注销).*(?:已回购|回购)股份|回购股份.*注销",
    ),
    _rule(
        "repurchase-change-v1",
        "event.repurchase_capital.repurchase_change",
        r"(?:调整|变更).*回购股份.*(?:方案|用途|价格|数量)|回购股份.*(?:方案|用途|价格|数量).*(?:调整|变更)",
    ),
    _rule(
        "repurchase-first-execution-v1",
        "event.repurchase_capital.repurchase_first_execution",
        r"首次(?:实施)?回购.*股份|首次回购公司股份",
    ),
    _rule(
        "repurchase-completion-v1",
        "event.repurchase_capital.repurchase_completion",
        r"回购股份.*(?:实施结果|实施完成|完成公告)|完成回购.*股份",
        excluded=r"(?:注销|终止|停止)",
    ),
    _rule(
        "repurchase-progress-v1",
        "event.repurchase_capital.repurchase_progress",
        r"回购股份.*(?:进展|比例达到)|回购.*进展公告",
    ),
    _rule(
        "repurchase-approved-v1",
        "event.repurchase_capital.repurchase_approved",
        r"股东大会.*(?:审议通过|批准).*回购|回购.*(?:股东大会审议通过|获股东大会批准)",
    ),
    _rule(
        "repurchase-proposal-v1",
        "event.repurchase_capital.repurchase_proposal",
        r"回购(?:公司)?股份.*(?:预案|方案)|关于回购(?:公司)?股份的公告",
        excluded=r"(?:进展|首次|实施结果|完成|注销|终止|停止|调整|变更|股东大会审议通过)",
    ),
    # Equity incentives.
    _rule(
        "equity-incentive-grant-v1",
        "event.governance_personnel.equity_incentive_grant",
        r"(?:股权|限制性股票|股票期权)激励计划.*(?:首次授予|预留授予|授予登记|授予完成)|向.*授予.*(?:限制性股票|股票期权)",
        excluded=r"(?:归属|行权|解锁|解除限售)",
    ),
    _rule(
        "equity-incentive-plan-v1",
        "event.governance_personnel.equity_incentive_plan",
        r"(?:股权|限制性股票|股票期权)激励计划.*(?:草案|方案)",
        excluded=r"(?:首次授予|预留授予|授予登记|授予完成)",
    ),
    # Regulation, litigation and credit risks.
    _rule(
        "public-censure-v1",
        "event.regulation_risk.public_censure",
        r"公开谴责(?:决定书)?|受到公开谴责",
    ),
    _rule(
        "disciplinary-action-v1",
        "event.regulation_risk.disciplinary_action",
        r"纪律处分(?:决定书)?|受到纪律处分",
    ),
    _rule(
        "administrative-penalty-v1",
        "event.regulation_risk.administrative_penalty",
        r"行政处罚决定书|收到行政处罚",
        excluded=r"事先告知",
    ),
    _rule(
        "investigation-opened-v1",
        "event.regulation_risk.investigation_opened",
        r"(?:收到)?立案告知书|被立案调查|监管立案调查",
    ),
    _rule(
        "information-disclosure-violation-v1",
        "event.regulation_risk.information_disclosure_violation",
        r"信息披露(?:违法违规|违规)(?:认定|决定|结论)",
    ),
    _rule(
        "litigation-progress-v1",
        "event.litigation_credit.litigation_progress",
        r"(?:重大诉讼|诉讼事项).*(?:进展|判决|裁决|结果)",
    ),
    _rule(
        "major-litigation-v1",
        "event.litigation_credit.major_litigation",
        r"(?:涉及|新增|发生)?重大诉讼",
        excluded=r"(?:进展|判决|裁决|结果)",
    ),
    _rule(
        "arbitration-v1",
        "event.litigation_credit.arbitration",
        r"重大仲裁|涉及仲裁事项",
        excluded=r"(?:进展|裁决|结果)",
    ),
    _rule(
        "debt-default-v1",
        "event.litigation_credit.debt_default",
        r"(?:债务|贷款|债券).*(?:逾期|违约|未能按期兑付)|未能按期兑付.*(?:债券|本息)",
    ),
    _rule(
        "credit-rating-downgrade-v1",
        "event.litigation_credit.credit_rating_downgrade",
        r"(?:主体|债项|信用)评级.*(?:下调|调降)|评级下调",
    ),
    _rule(
        "bankruptcy-reorganization-v1",
        "event.litigation_credit.bankruptcy_reorganization",
        r"(?:申请|受理|进入).*(?:破产重整|破产清算)|(?:破产重整|破产清算).*(?:申请|受理|进展)",
    ),
    # Governance changes.  Deputy-manager-only announcements are excluded.
    _rule(
        "chairman-change-v1",
        "event.governance_personnel.chairman_change",
        r"董事长.*(?:辞职|辞任|离任|变更|选举|选定)|(?:选举|选定|变更).*董事长",
    ),
    _rule(
        "ceo-change-v1",
        "event.governance_personnel.ceo_change",
        r"总经理.*(?:辞职|辞任|离任|变更)|(?:聘任|任命|变更).*总经理",
        excluded=r"副总经理|总经理助理",
    ),
    _rule(
        "cfo-change-v1",
        "event.governance_personnel.cfo_change",
        r"(?:财务负责人|财务总监).*(?:辞职|辞任|离任|变更)|(?:聘任|任命|变更).*(?:财务负责人|财务总监)",
    ),
    _rule(
        "board-secretary-change-v1",
        "event.governance_personnel.board_secretary_change",
        r"董事会秘书.*(?:辞职|辞任|离任|变更)|(?:聘任|任命|变更).*董事会秘书",
    ),
    _rule(
        "director-resignation-v1",
        "event.governance_personnel.director_resignation",
        r"(?:董事|独立董事).*(?:辞职|辞任)",
        excluded=r"(?:董事长|董事会秘书)",
    ),
    _rule(
        "supervisor-resignation-v1",
        "event.governance_personnel.supervisor_resignation",
        r"监事.*(?:辞职|辞任)",
    ),
    # M&A lifecycle.
    _rule(
        "restructuring-terminated-v1",
        "event.m_and_a_restructuring.restructuring_terminated",
        r"终止.*(?:重大资产重组|发行股份购买资产)|(?:重大资产重组|发行股份购买资产).*终止",
    ),
    _rule(
        "restructuring-completed-v1",
        "event.m_and_a_restructuring.restructuring_completed",
        r"(?:重大资产重组|发行股份购买资产).*(?:实施完成|交割完成)|(?:实施完成|交割完成).*(?:重大资产重组|发行股份购买资产)",
    ),
    _rule(
        "restructuring-regulatory-approval-v1",
        "event.m_and_a_restructuring.restructuring_regulatory_approval",
        r"(?:重大资产重组|发行股份购买资产).*(?:获|取得).*(?:证监会|交易所).*(?:同意|批准|注册)|(?:证监会|交易所).*(?:同意|批准|注册).*(?:重大资产重组|发行股份购买资产)",
    ),
    _rule(
        "restructuring-shareholder-approval-v1",
        "event.m_and_a_restructuring.restructuring_approved_shareholders",
        r"股东大会.*审议通过.*(?:重大资产重组|发行股份购买资产)|(?:重大资产重组|发行股份购买资产).*股东大会审议通过",
    ),
    _rule(
        "restructuring-board-approval-v1",
        "event.m_and_a_restructuring.restructuring_approved_board",
        r"董事会.*审议通过.*(?:重大资产重组|发行股份购买资产)|(?:重大资产重组|发行股份购买资产).*董事会审议通过",
    ),
    _rule(
        "restructuring-resumption-v1",
        "event.m_and_a_restructuring.restructuring_resumption",
        r"(?:重大资产重组|筹划重组).*复牌|复牌.*(?:重大资产重组|筹划重组)",
    ),
    _rule(
        "restructuring-suspension-v1",
        "event.m_and_a_restructuring.restructuring_suspension",
        r"(?:重大资产重组|筹划重组).*停牌|停牌.*(?:重大资产重组|筹划重组)",
    ),
    _rule(
        "spin-off-listing-v1",
        "event.m_and_a_restructuring.spin_off_listing",
        r"分拆.*上市",
    ),
    _rule(
        "asset-sale-plan-v1",
        "event.m_and_a_restructuring.asset_sale_plan",
        r"(?:重大资产出售|出售重大资产).*(?:预案|方案)",
    ),
    _rule(
        "acquisition-plan-v1",
        "event.m_and_a_restructuring.acquisition_plan",
        r"(?:发行股份购买资产|重大资产收购|收购重大资产).*(?:预案|方案)",
    ),
    # Material contracts and orders.  Generic contracts remain unclassified.
    _rule(
        "major-contract-terminated-v1",
        "event.contracts_orders.contract_terminated",
        r"(?:重大合同|重大经营合同).*(?:终止|解除)|(?:终止|解除).*(?:重大合同|重大经营合同)",
    ),
    _rule(
        "major-contract-changed-v1",
        "event.contracts_orders.contract_changed",
        r"(?:重大合同|重大经营合同).*(?:变更|调整)|(?:变更|调整).*(?:重大合同|重大经营合同)",
    ),
    _rule(
        "major-contract-completed-v1",
        "event.contracts_orders.contract_completed",
        r"(?:重大合同|重大经营合同).*(?:履行完毕|完成履行|完成交付)",
    ),
    _rule(
        "major-contract-progress-v1",
        "event.contracts_orders.contract_progress",
        r"(?:重大合同|重大经营合同).*(?:进展|履行情况)",
    ),
    _rule(
        "major-contract-won-v1",
        "event.contracts_orders.major_contract_won",
        r"重大项目.*(?:中标|中选)|(?:中标|中选).*重大项目",
    ),
    _rule(
        "major-contract-signed-v1",
        "event.contracts_orders.major_contract_signed",
        r"(?:签署|签订).*(?:重大合同|重大经营合同)|(?:重大合同|重大经营合同).*(?:签署|签订)",
    ),
    _rule(
        "framework-agreement-v1",
        "event.contracts_orders.framework_agreement",
        r"(?:签署|签订).*(?:战略合作|框架)协议|(?:战略合作|框架)协议.*(?:签署|签订)",
    ),
    _rule(
        "major-order-received-v1",
        "event.contracts_orders.purchase_order_received",
        r"(?:收到|获得|取得).*重大订单|重大订单.*(?:收到|获得|取得)",
    ),
)


def eastmoney_event_coverage_contract(event_code: str) -> EastmoneyEventCoverageContract:
    """Return the current code-derived acquisition contract for one event code."""

    provider_columns = eastmoney_provider_column_codes(event_code)
    rules = tuple(
        rule
        for rule in _TITLE_EVENT_RULES
        if rule.event_code == event_code and _title_rule_has_acceptance_evidence(rule)
    )
    rule_ids = tuple(rule.rule_id for rule in rules)
    query_succeeded = bool(provider_columns or rules)
    if provider_columns:
        coverage_basis = COMPLETE_PROVIDER_COLUMN_COVERAGE_BASIS
    elif rules:
        coverage_basis = COMPLETE_DETERMINISTIC_TITLE_COVERAGE_BASIS
    else:
        coverage_basis = "no_deterministic_announcement_classifier"
    classifier_contract: dict[str, object] = {
        "classifierVersion": ANNOUNCEMENT_CLASSIFIER_VERSION,
        "eventCode": event_code,
        "normalization": "unicode_nfkc_remove_whitespace.v1",
        "providerColumnCodes": list(provider_columns),
        "titleRules": [
            {
                "ruleId": rule.rule_id,
                "pattern": rule.pattern.pattern,
                "excluded": rule.excluded.pattern if rule.excluded is not None else None,
                "acceptance": {
                    "positiveTitle": TITLE_RULE_ACCEPTANCE_CASES[rule.rule_id].positive_title,
                    "revisedNegativeTitle": TITLE_RULE_ACCEPTANCE_CASES[
                        rule.rule_id
                    ].revised_negative_title,
                    "confusableNegativeTitles": list(
                        TITLE_RULE_ACCEPTANCE_CASES[rule.rule_id].confusable_negative_titles
                    ),
                },
            }
            for rule in rules
        ],
        "globalGuards": {
            "ambiguousOrNegatedTitle": _AMBIGUOUS_OR_NEGATED_TITLE_RE.pattern,
            "revisedOrWithdrawnTitle": _REVISED_OR_WITHDRAWN_TITLE_RE.pattern,
            "periodicDocumentRevision": _NON_INITIAL_PERIODIC_DOCUMENT_RE.pattern,
            "firstAvailability": "validated_second_precision_Asia/Shanghai.v1",
        },
    }
    return EastmoneyEventCoverageContract(
        event_code=event_code,
        query_succeeded=query_succeeded,
        coverage_basis=coverage_basis,
        provider_column_codes=provider_columns,
        title_rule_ids=rule_ids,
        classifier_sha256=hashlib.sha256(_canonical_json_bytes(classifier_contract)).hexdigest(),
    )


def eastmoney_preparable_event_codes() -> frozenset[str]:
    """Codes backed by the current all-announcement classifier contract.

    This deliberately derives from concrete provider columns and title rules,
    not from the broader executable/Catalog registry.  A newly catalogued code
    therefore remains compile-only until a collector classifier and its
    positive/confusable-negative tests are added.
    """

    candidates = {
        *_COLUMN_EVENT_CODES.values(),
        *(rule.event_code for rule in _TITLE_EVENT_RULES),
    }
    return frozenset(
        event_code
        for event_code in candidates
        if eastmoney_event_coverage_contract(event_code).query_succeeded
    )


def _title_rule_has_acceptance_evidence(rule: _TitleEventRule) -> bool:
    case = TITLE_RULE_ACCEPTANCE_CASES.get(rule.rule_id)
    if case is None or case.event_code != rule.event_code:
        return False
    unclassified = (_UNCLASSIFIED_EVENT_CODE, "unclassified", None)
    return (
        _classify_normalized_title(
            _normalized_title(case.positive_title),
            rules=_TITLE_EVENT_RULES,
        )
        == (rule.event_code, "deterministic_title_rule", rule.rule_id)
        and _classify_normalized_title(
            _normalized_title(case.revised_negative_title),
            rules=_TITLE_EVENT_RULES,
        )
        == unclassified
        and bool(case.confusable_negative_titles)
        and all(
            _classify_normalized_title(
                _normalized_title(title),
                rules=_TITLE_EVENT_RULES,
            )
            == unclassified
            for title in case.confusable_negative_titles
        )
    )


class EastmoneyAnnouncementError(RuntimeError):
    """The public announcement response cannot be replayed without ambiguity."""


@dataclass(frozen=True, slots=True)
class _RawAnnouncement:
    payload: Mapping[str, object]
    raw_response_sha256: str
    content: _RawAnnouncementContent | None = None


@dataclass(frozen=True, slots=True)
class _RawAnnouncementContent:
    payload: Mapping[str, object]
    raw_response_sha256: str
    document_url: str
    document_sha256: str
    document_hash_basis: str
    pdf_response_sha256: str | None = None
    extracted_text: ExtractedDocumentText | None = None


@dataclass(frozen=True, slots=True)
class _AnnouncementPageEvidence:
    page_index: int
    row_count: int
    response_sha256: str


@dataclass(frozen=True, slots=True)
class _AnnouncementQueryResult:
    records: tuple[_RawAnnouncement, ...]
    total_hits: int
    pages: tuple[_AnnouncementPageEvidence, ...]


class EastmoneyAnnouncementSource:
    """Fetch immutable Eastmoney announcement observations.

    A caller may inject an existing :class:`httpx.Client` or a transport.  Unit
    tests inject :class:`httpx.MockTransport`, so constructing this source does
    not itself perform network I/O.
    """

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout = _DEFAULT_TIMEOUT_SECONDS,
        page_size: int = _MAX_PROVIDER_PAGE_SIZE,
        max_pages: int = _DEFAULT_MAX_PAGES,
        extract_document_text: bool = False,
        document_text_extractor: AnnouncementDocumentTextExtractor | None = None,
        request_throttle: HostThrottle | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("inject client or transport, not both")
        if type(page_size) is not int or not 1 <= page_size <= _MAX_PROVIDER_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {_MAX_PROVIDER_PAGE_SIZE}")
        if type(max_pages) is not int or max_pages <= 0:
            raise ValueError("max_pages must be a positive integer")
        if type(extract_document_text) is not bool:
            raise ValueError("extract_document_text must be a boolean")
        if document_text_extractor is not None and not extract_document_text:
            raise ValueError("document_text_extractor requires extract_document_text=True")

        self._timeout = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
        self._page_size = page_size
        self._max_pages = max_pages
        self._document_text_extractor: AnnouncementDocumentTextExtractor | None = None
        if extract_document_text:
            self._document_text_extractor = (
                document_text_extractor or AnnouncementDocumentTextExtractor()
            )
        self._http = HostThrottledHttpClient(
            client=client,
            transport=transport,
            timeout=self._timeout,
            throttle=request_throttle,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> EastmoneyAnnouncementSource:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def fetch(
        self,
        *,
        instrument_id: InstrumentId,
        start: date,
        end: date,
        retrieved_at: datetime,
    ) -> tuple[EventObservation, ...]:
        """Return chronologically ordered, provider-ID-deduplicated observations."""

        return self.fetch_batch(
            instrument_id=instrument_id,
            start=start,
            end=end,
            retrieved_at=retrieved_at,
        ).observations

    def fetch_batch(
        self,
        *,
        instrument_id: InstrumentId,
        start: date,
        end: date,
        retrieved_at: datetime,
        requested_event_codes: Sequence[str] | None = None,
        requested_provider_column_codes: Sequence[str] | None = None,
    ) -> EventFetchBatch:
        """Return observations and immutable proof that pagination completed.

        When event codes are provided, content/PDF retrieval is limited to list
        rows classified into those codes.  Every announcement-list page is still
        fetched and hashed, so a zero-result claim is tied to the complete
        provider interval query rather than to the number of retained events.
        """

        if start > end:
            raise ValueError("start must not exceed end")
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise ValueError("retrieved_at must include an explicit timezone")
        stock_code = _stock_code(instrument_id)
        codes = tuple(sorted(set(requested_event_codes or ())))
        if any(not item.startswith("event.") for item in codes):
            raise ValueError("requested_event_codes must contain canonical event.* codes")
        provider_column_codes = tuple(sorted(set(requested_provider_column_codes or ())))
        if any(
            not item or not item.isascii() or not item.isdigit() for item in provider_column_codes
        ):
            raise ValueError(
                "requested_provider_column_codes must contain numeric provider column codes"
            )
        if codes and provider_column_codes:
            raise ValueError(
                "requested_event_codes and requested_provider_column_codes are mutually exclusive"
            )
        page_result = self._fetch_pages(
            stock_code=stock_code,
            compatible_market_codes=_compatible_market_codes(instrument_id),
            start=start,
            end=end,
        )
        list_records = page_result.records
        if codes:
            list_records = tuple(
                record for record in list_records if _classified_event_code(record.payload) in codes
            )
        if provider_column_codes:
            requested_columns = frozenset(provider_column_codes)
            list_records = tuple(
                record
                for record in list_records
                if _has_provider_column(record.payload, requested_columns)
            )
        records = tuple(
            _RawAnnouncement(
                payload=record.payload,
                raw_response_sha256=record.raw_response_sha256,
                content=self._fetch_content(
                    art_code=_required_text(record.payload.get("art_code"), "art_code")
                ),
            )
            for record in list_records
        )

        observations = [
            _to_observation(
                record,
                instrument_id=instrument_id,
                retrieved_at=retrieved_at,
            )
            for record in records
        ]
        _validate_unique_initial_periodic_reports(observations)
        observations.sort(key=_observation_sort_key)
        evidence = _query_evidence(
            instrument_id=instrument_id,
            stock_code=stock_code,
            start=start,
            end=end,
            page_size=self._page_size,
            query=page_result,
            requested_event_codes=codes,
            requested_provider_column_codes=provider_column_codes,
            selected_record_count=len(list_records),
        )
        return EventFetchBatch(
            observations=tuple(observations),
            acquisition_evidence=evidence,
        )

    def _fetch_pages(
        self,
        *,
        stock_code: str,
        compatible_market_codes: frozenset[str],
        start: date,
        end: date,
    ) -> _AnnouncementQueryResult:
        by_art_code: dict[str, _RawAnnouncement] = {}
        expected_total: int | None = None
        page_evidence: list[_AnnouncementPageEvidence] = []

        for page_index in range(1, self._max_pages + 1):
            response = self._request_page(
                stock_code=stock_code,
                start=start,
                end=end,
                page_index=page_index,
            )
            response_digest = hashlib.sha256(response.content).hexdigest()
            data = _response_data(response)
            if data.get("page_index") != page_index or data.get("page_size") != self._page_size:
                raise EastmoneyAnnouncementError(
                    "announcement response page identity does not match the requested page"
                )
            total_hits = _required_non_negative_int(data.get("total_hits"), "data.total_hits")
            if expected_total is None:
                expected_total = total_hits
                required_pages = (total_hits + self._page_size - 1) // self._page_size
                if required_pages > self._max_pages:
                    raise EastmoneyAnnouncementError(
                        "announcement result exceeds configured max_pages; "
                        "refusing a partial backfill"
                    )
            elif total_hits != expected_total:
                raise EastmoneyAnnouncementError(
                    "announcement total_hits changed during pagination; "
                    "retry a fixed historical range"
                )

            raw_items = data.get("list")
            if not isinstance(raw_items, list):
                raise EastmoneyAnnouncementError("data.list must be an array")
            items = cast(list[object], raw_items)
            if len(items) > self._page_size:
                raise EastmoneyAnnouncementError(
                    "announcement page returned more rows than the requested page_size"
                )
            page_evidence.append(
                _AnnouncementPageEvidence(
                    page_index=page_index,
                    row_count=len(items),
                    response_sha256=response_digest,
                )
            )
            if expected_total == 0:
                if items:
                    raise EastmoneyAnnouncementError(
                        "zero total_hits returned with non-empty data.list"
                    )
                return _AnnouncementQueryResult(
                    records=(),
                    total_hits=0,
                    pages=tuple(page_evidence),
                )
            if not items:
                raise EastmoneyAnnouncementError(
                    "announcement pagination ended before total_hits was reached"
                )

            for offset, raw_item in enumerate(items):
                item = _string_mapping(raw_item, f"data.list[{offset}]")
                notice_day = _parse_notice_date(
                    _required_text(item.get("notice_date"), "notice_date")
                )
                # Eastmoney labels an after-close release with the next notice
                # day in some historical rows.  One next-day label is therefore
                # part of the explicit provider interval semantics; anything
                # earlier or farther out is a scope violation.
                if notice_day < start or notice_day > end + timedelta(days=1):
                    raise EastmoneyAnnouncementError(
                        "announcement row is outside the requested date interval"
                    )
                _validate_returned_instrument(
                    item.get("codes"),
                    stock_code=stock_code,
                    compatible_market_codes=compatible_market_codes,
                )
                art_code = _required_text(item.get("art_code"), "art_code")
                _validate_art_code(art_code)
                record = _RawAnnouncement(
                    payload=item,
                    raw_response_sha256=response_digest,
                )
                previous = by_art_code.get(art_code)
                if previous is not None and _canonical_json(previous.payload) != _canonical_json(
                    item
                ):
                    raise EastmoneyAnnouncementError(
                        "announcement art_code changed during pagination; refusing an "
                        "ambiguous revision"
                    )
                by_art_code.setdefault(art_code, record)

            unique_total = len(by_art_code)
            if unique_total > expected_total:
                raise EastmoneyAnnouncementError(
                    "unique announcement art_code count exceeded total_hits"
                )
            if unique_total == expected_total:
                return _AnnouncementQueryResult(
                    records=tuple(by_art_code.values()),
                    total_hits=expected_total,
                    pages=tuple(page_evidence),
                )
            if len(items) < self._page_size:
                raise EastmoneyAnnouncementError(
                    "short announcement page returned before unique art_code coverage "
                    "reached total_hits"
                )

        raise EastmoneyAnnouncementError(
            "announcement pagination reached max_pages before unique art_code coverage "
            "reached total_hits"
        )

    def _request_page(
        self,
        *,
        stock_code: str,
        start: date,
        end: date,
        page_index: int,
    ) -> httpx.Response:
        params = {
            "sr": "-1",
            "page_size": str(self._page_size),
            "page_index": str(page_index),
            "ann_type": "A",
            "client_source": "web",
            "f_node": "0",
            "s_node": "0",
            "stock_list": stock_code,
            "begin_time": start.isoformat(),
            "end_time": end.isoformat(),
        }
        try:
            response = self._http.get(
                ANNOUNCEMENT_LIST_URL,
                min_interval=_MIN_REQUEST_INTERVAL_SECONDS,
                params=params,
                headers=_HEADERS,
                timeout=self._timeout,
            )
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            raise EastmoneyAnnouncementError(
                f"Eastmoney announcement page {page_index} failed: {exc}"
            ) from exc

    def _fetch_content(self, *, art_code: str) -> _RawAnnouncementContent:
        _validate_art_code(art_code)
        source_url = _content_url(art_code)
        try:
            response = self._http.get(
                ANNOUNCEMENT_CONTENT_URL,
                min_interval=_MIN_REQUEST_INTERVAL_SECONDS,
                params={
                    "art_code": art_code,
                    "client_source": "web",
                    "page_index": "1",
                },
                headers=_HEADERS,
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise EastmoneyAnnouncementError(
                f"Eastmoney announcement content {art_code} failed: {exc}"
            ) from exc

        response_sha256 = hashlib.sha256(response.content).hexdigest()
        data = _content_response_data(response)
        content_art_code = _required_text(data.get("art_code"), "content.data.art_code")
        if content_art_code != art_code:
            raise EastmoneyAnnouncementError(
                "announcement content art_code does not match list response"
            )

        notice_content = _optional_raw_text(
            data.get("notice_content"),
            "content.data.notice_content",
        )
        page_count = _required_non_negative_int(
            data.get("page_size"),
            "content.data.page_size",
        )
        pdf_url = _content_pdf_url(data, art_code=art_code)
        if notice_content is not None and page_count == 1:
            document_bytes = notice_content.encode("utf-8")
            document_sha256 = hashlib.sha256(document_bytes).hexdigest()
            extracted_text = self._extract_notice_text(
                art_code=art_code,
                notice_content=notice_content,
            )
            return _RawAnnouncementContent(
                payload=data,
                raw_response_sha256=response_sha256,
                document_url=source_url,
                document_sha256=document_sha256,
                document_hash_basis="notice_content_utf8",
                extracted_text=extracted_text,
            )

        if pdf_url is None:
            raise EastmoneyAnnouncementError(
                f"announcement content {art_code} does not expose a complete document"
            )
        pdf_bytes = self._fetch_pdf(pdf_url=pdf_url, art_code=art_code)
        pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
        extracted_text = self._extract_pdf_text(
            art_code=art_code,
            pdf_bytes=pdf_bytes,
            expected_page_count=page_count,
        )
        return _RawAnnouncementContent(
            payload=data,
            raw_response_sha256=response_sha256,
            document_url=pdf_url,
            document_sha256=pdf_sha256,
            document_hash_basis="pdf_bytes",
            pdf_response_sha256=pdf_sha256,
            extracted_text=extracted_text,
        )

    def _extract_notice_text(
        self,
        *,
        art_code: str,
        notice_content: str,
    ) -> ExtractedDocumentText | None:
        if self._document_text_extractor is None:
            return None
        try:
            return self._document_text_extractor.extract_notice_content(notice_content)
        except DocumentTextExtractionError as exc:
            raise EastmoneyAnnouncementError(
                f"announcement document text {art_code} failed: {exc}"
            ) from exc

    def _extract_pdf_text(
        self,
        *,
        art_code: str,
        pdf_bytes: bytes,
        expected_page_count: int,
    ) -> ExtractedDocumentText | None:
        if self._document_text_extractor is None:
            return None
        try:
            return self._document_text_extractor.extract_pdf(
                pdf_bytes,
                expected_page_count=expected_page_count,
            )
        except DocumentTextExtractionError as exc:
            raise EastmoneyAnnouncementError(
                f"announcement document text {art_code} failed: {exc}"
            ) from exc

    def _fetch_pdf(self, *, pdf_url: str, art_code: str) -> bytes:
        validated_url = _validated_pdf_url(pdf_url, art_code=art_code)
        try:
            response = self._http.get(
                validated_url,
                min_interval=_MIN_REQUEST_INTERVAL_SECONDS,
                headers=_HEADERS,
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise EastmoneyAnnouncementError(
                f"Eastmoney announcement PDF {art_code} failed: {exc}"
            ) from exc
        if not response.content.startswith(b"%PDF-"):
            raise EastmoneyAnnouncementError(
                f"Eastmoney announcement PDF {art_code} is not a PDF document"
            )
        return response.content


def _classified_event_code(raw: Mapping[str, object]) -> str:
    return _event_classification(
        _columns(raw.get("columns")),
        title=_required_text(raw.get("title"), "title"),
    )[0]


def _has_provider_column(
    raw: Mapping[str, object],
    requested_columns: frozenset[str],
) -> bool:
    return any(
        column["column_code"] in requested_columns for column in _columns(raw.get("columns"))
    )


def _classification_summary(query: _AnnouncementQueryResult) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    for record in query.records:
        title = _required_text(record.payload.get("title"), "title")
        event_code, method, rule_id = _event_classification(
            _columns(record.payload.get("columns")),
            title=title,
        )
        art_code = _required_text(record.payload.get("art_code"), "art_code")
        rows.append(
            {
                "artCode": art_code,
                "eventCode": event_code,
                "method": method,
                "ruleId": rule_id,
                "listResponseSha256": record.raw_response_sha256,
            }
        )
        counts[event_code] += 1
    rows.sort(key=lambda item: cast(str, item["artCode"]))
    unclassified = counts.pop(_UNCLASSIFIED_EVENT_CODE, 0)
    body: dict[str, object] = {
        "classifierVersion": ANNOUNCEMENT_CLASSIFIER_VERSION,
        "totalRows": len(query.records),
        "classifiedRows": len(query.records) - unclassified,
        "unclassifiedRows": unclassified,
        "rowsByEventCode": dict(sorted(counts.items())),
        "classifiedRowsSha256": hashlib.sha256(_canonical_json_bytes(rows)).hexdigest(),
    }
    body["evidenceSha256"] = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    return body


def _query_evidence(
    *,
    instrument_id: InstrumentId,
    stock_code: str,
    start: date,
    end: date,
    page_size: int,
    query: _AnnouncementQueryResult,
    requested_event_codes: Sequence[str],
    requested_provider_column_codes: Sequence[str],
    selected_record_count: int,
) -> Mapping[str, object]:
    request_parameters: dict[str, object] = {
        "sr": "-1",
        "pageSize": page_size,
        "annType": "A",
        "clientSource": "web",
        "fNode": "0",
        "sNode": "0",
        "stockList": stock_code,
        "beginTime": start.isoformat(),
        "endTime": end.isoformat(),
    }
    pages = [
        {
            "pageIndex": item.page_index,
            "rowCount": item.row_count,
            "responseSha256": item.response_sha256,
        }
        for item in query.pages
    ]
    pagination_body: dict[str, object] = {
        "pageSize": page_size,
        "pageCount": len(pages),
        "totalHits": query.total_hits,
        "uniqueArtCodes": len(query.records),
        "returnedRows": sum(item.row_count for item in query.pages),
        "complete": len(query.records) == query.total_hits,
        "zeroResult": query.total_hits == 0,
        "pages": pages,
    }
    pagination_body["evidenceSha256"] = hashlib.sha256(
        _canonical_json_bytes(pagination_body)
    ).hexdigest()
    classification_summary = _classification_summary(query)
    rows_by_event_code = cast(
        Mapping[str, int],
        classification_summary["rowsByEventCode"],
    )
    if requested_event_codes and selected_record_count != sum(
        rows_by_event_code.get(event_code, 0) for event_code in requested_event_codes
    ):
        raise EastmoneyAnnouncementError(
            "selected announcement rows do not reconcile with deterministic classification"
        )
    event_code_coverage: dict[str, object] = {}
    for event_code in requested_event_codes:
        contract = eastmoney_event_coverage_contract(event_code)
        event_code_coverage[event_code] = {
            "querySucceeded": contract.query_succeeded,
            "coverageBasis": contract.coverage_basis,
            "providerColumnCodes": list(contract.provider_column_codes),
            "titleRuleIds": list(contract.title_rule_ids),
            "classifierVersion": ANNOUNCEMENT_CLASSIFIER_VERSION,
            "classifierSha256": contract.classifier_sha256,
            "selectedRecordCount": rows_by_event_code.get(event_code, 0),
        }
    body: dict[str, object] = {
        "schemaVersion": "ashare-lab.eastmoney-announcement-query-evidence.v2",
        "provider": "eastmoney",
        "instrumentId": instrument_id.value,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "endpoint": ANNOUNCEMENT_LIST_URL,
        "requestParameters": request_parameters,
        "requestSha256": hashlib.sha256(_canonical_json_bytes(request_parameters)).hexdigest(),
        "querySucceeded": pagination_body["complete"],
        "pagination": pagination_body,
        "classificationSummary": classification_summary,
        "eventCodeCoverage": event_code_coverage,
    }
    if requested_provider_column_codes:
        body["providerColumnSelection"] = {
            "requestedColumnCodes": list(requested_provider_column_codes),
            "selectedRecordCount": selected_record_count,
            "selectionAppliedAfterCompletePagination": True,
        }
    body["evidenceSha256"] = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    return body


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _response_data(response: httpx.Response) -> Mapping[str, object]:
    try:
        decoded: object = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EastmoneyAnnouncementError("announcement response is not valid JSON") from exc
    payload = _string_mapping(decoded, "response")
    if payload.get("success") != 1:
        error = payload.get("error")
        raise EastmoneyAnnouncementError(f"Eastmoney announcement API rejected request: {error!r}")
    return _string_mapping(payload.get("data"), "response.data")


def _content_response_data(response: httpx.Response) -> Mapping[str, object]:
    try:
        decoded: object = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EastmoneyAnnouncementError("announcement content response is not valid JSON") from exc
    payload = _string_mapping(decoded, "content.response")
    if payload.get("success") != 1:
        raise EastmoneyAnnouncementError("Eastmoney announcement content API rejected request")
    return _string_mapping(payload.get("data"), "content.response.data")


def _to_observation(
    record: _RawAnnouncement,
    *,
    instrument_id: InstrumentId,
    retrieved_at: datetime,
) -> EventObservation:
    raw = record.payload
    content = record.content
    if content is None:
        raise EastmoneyAnnouncementError("announcement content was not frozen")
    raw_content = content.payload
    art_code = _required_text(raw.get("art_code"), "art_code")
    _validate_art_code(art_code)
    title = _required_text(raw.get("title"), "title")
    notice_day = _parse_notice_date(_required_text(raw.get("notice_date"), "notice_date"))

    raw_display_time = _optional_text(raw.get("display_time"), "display_time")
    parsed_display_time = _parse_optional_provider_datetime(raw_display_time)
    credible_display_time = (
        _ceil_second(parsed_display_time)
        if parsed_display_time is not None
        and _is_plausible_provider_time(parsed_display_time, notice_day)
        else None
    )

    raw_ei_time = _optional_text(raw.get("eiTime"), "eiTime")
    parsed_ei_time = _parse_optional_provider_datetime(raw_ei_time)
    credible_list_ei_time = (
        parsed_ei_time
        if parsed_ei_time is not None
        and parsed_ei_time.microsecond == 0
        and _is_plausible_provider_time(parsed_ei_time, notice_day)
        else None
    )

    raw_content_ei_time = _optional_text(raw_content.get("eitime"), "content.data.eitime")
    parsed_content_ei_time = _parse_optional_provider_datetime(raw_content_ei_time)
    credible_content_ei_time = (
        parsed_content_ei_time
        if parsed_content_ei_time is not None
        and parsed_content_ei_time.microsecond == 0
        and _is_plausible_provider_time(parsed_content_ei_time, notice_day)
        else None
    )
    invalid_provider_time = any(
        (
            raw_display_time is not None and credible_display_time is None,
            raw_ei_time is not None and credible_list_ei_time is None,
            raw_content_ei_time is not None and credible_content_ei_time is None,
        )
    )
    credible_ei_times = tuple(
        value for value in (credible_list_ei_time, credible_content_ei_time) if value is not None
    )
    credible_ei_time = max(credible_ei_times) if credible_ei_times else None

    if invalid_provider_time:
        source_released_at = credible_display_time or datetime.combine(
            notice_day,
            _DATE_ONLY_CLOSE,
            tzinfo=_SHANGHAI,
        )
        time_quality = TimeQuality.DATE_ONLY_CONSERVATIVE
        validation_status = "rejected"
    elif credible_ei_time is not None or credible_display_time is not None:
        source_released_at = credible_display_time
        time_quality = TimeQuality.VENDOR_OBSERVED
        validation_status = "validated"
    else:
        source_released_at = datetime.combine(
            notice_day,
            _DATE_ONLY_CLOSE,
            tzinfo=_SHANGHAI,
        )
        time_quality = TimeQuality.DATE_ONLY_CONSERVATIVE
        validation_status = "unverified"

    columns = _columns(raw.get("columns"))
    event_code, classification_method, classification_rule_id = _event_classification(
        columns,
        title=title,
    )
    document_url = content.document_url
    source_url = _content_url(art_code)
    content_title = _required_text(
        raw_content.get("notice_title"),
        "content.data.notice_title",
    )
    if _normalized_title(content_title) != _normalized_title(title):
        raise EastmoneyAnnouncementError("announcement content title does not match list response")
    raw_payload_json = _canonical_json(raw)
    raw_content_payload_json = _canonical_json(raw_content)
    raw_columns_json = _canonical_json(columns)
    raw_codes_json = _canonical_json(raw.get("codes") if raw.get("codes") is not None else [])
    notice_content = _optional_raw_text(
        raw_content.get("notice_content"),
        "content.data.notice_content",
    )
    notice_content_sha256 = (
        hashlib.sha256(notice_content.encode("utf-8")).hexdigest()
        if notice_content is not None
        else None
    )
    conservative_available_at = max(
        value for value in (source_released_at, credible_ei_time) if value is not None
    )
    attributes: dict[str, str | int | None] = {
        "provider": "eastmoney",
        "source": "eastmoney",
        "source_event_id": art_code,
        "source_url": source_url,
        "document_url": document_url,
        "ingested_at": retrieved_at.isoformat(),
        "time_quality": time_quality.value,
        "validation_status": validation_status,
        "raw_response_sha256": record.raw_response_sha256,
        "raw_content_response_sha256": content.raw_response_sha256,
        "document_hash_basis": content.document_hash_basis,
        "classification_method": classification_method,
        "classification_rule_id": classification_rule_id,
        "classification_contract_sha256": eastmoney_event_coverage_contract(
            event_code
        ).classifier_sha256,
        "event_lifecycle_code": event_code,
        "announcement_revision_role": "initial_or_explicit_lifecycle",
        "revision_policy": "provider_art_code_revision0_revisions_withdrawals_unclassified",
        "conservative_available_at": conservative_available_at.isoformat(),
        "timing_policy": "max(ceil(display_time), credible_eitime)",
        "raw_art_code": art_code,
        "raw_title": title,
        "raw_ei_time": raw_ei_time,
        "raw_content_ei_time": raw_content_ei_time,
        "raw_display_time": raw_display_time,
        "raw_notice_date": notice_day.isoformat(),
        "raw_sort_date": _optional_text(raw.get("sort_date"), "sort_date"),
        "raw_source_type": _optional_text(raw.get("source_type"), "source_type"),
        "raw_columns_json": raw_columns_json,
        "raw_codes_json": raw_codes_json,
        "raw_payload_json": raw_payload_json,
        "raw_content_payload_json": raw_content_payload_json,
        "raw_notice_content_sha256": notice_content_sha256,
        "raw_pdf_response_sha256": content.pdf_response_sha256,
        "raw_content_notice_title": _optional_text(
            raw_content.get("notice_title"),
            "content.data.notice_title",
        ),
        "raw_content_notice_date": _optional_text(
            raw_content.get("notice_date"),
            "content.data.notice_date",
        ),
    }
    attributes.update(_periodic_report_attributes(event_code, title=title))
    extracted_text = content.extracted_text
    if extracted_text is not None:
        if event_code in DOCUMENT_TEXT_EVENT_CODES and not _is_initial_complete_document_title(
            event_code,
            _normalized_title(content_title),
        ):
            raise EastmoneyAnnouncementError(
                "announcement content is not the initial complete periodic report"
            )
        if extracted_text.source_document_sha256 != content.document_sha256:
            raise EastmoneyAnnouncementError(
                "extracted announcement text is not tied to the frozen document hash"
            )
        provider_page_count = _required_non_negative_int(
            raw_content.get("page_size"),
            "content.data.page_size",
        )
        if provider_page_count != extracted_text.page_count:
            raise EastmoneyAnnouncementError(
                "extracted announcement page count does not match provider metadata"
            )
        attributes.update(
            {
                "document_text": extracted_text.text,
                "document_text_sha256": extracted_text.text_sha256,
                "document_text_source_sha256": extracted_text.source_document_sha256,
                "document_text_source_format": extracted_text.source_format,
                "document_text_extractor": extracted_text.extractor_name,
                "document_text_extractor_version": extracted_text.extractor_version,
                "document_text_normalization": extracted_text.normalization,
                "document_text_quality": extracted_text.quality_status,
                "document_text_page_count": extracted_text.page_count,
                "document_text_provider_page_count": provider_page_count,
                "document_text_extracted_page_count": extracted_text.page_count,
                "document_text_empty_page_count": 0,
                "document_text_character_count": extracted_text.character_count,
                "document_text_non_whitespace_character_count": (
                    extracted_text.non_whitespace_character_count
                ),
                "document_text_pages_json": extracted_text.pages_json(),
            }
        )
        if extracted_text.extractor_library_version is not None:
            attributes["document_text_extractor_library_version"] = (
                extracted_text.extractor_library_version
            )

    return EventObservation(
        provider="eastmoney",
        provider_event_id=art_code,
        instrument_id=instrument_id,
        event_code=event_code,
        title=title,
        occurred_at=None,
        source_released_at=source_released_at,
        vendor_first_available_at=credible_ei_time,
        retrieved_at=retrieved_at,
        time_quality=time_quality,
        document_url=document_url,
        document_sha256=content.document_sha256,
        attributes=attributes,
        revision_no=0,
        validation_status=validation_status,
        raw_response_sha256=record.raw_response_sha256,
    )


def _stock_code(instrument_id: InstrumentId) -> str:
    value = str(instrument_id)
    try:
        code, exchange = value.split(".", maxsplit=1)
    except ValueError as exc:
        raise ValueError("instrument_id must use the canonical 000000.EXCHANGE form") from exc
    if len(code) != 6 or not code.isdigit() or exchange not in {"SZ", "SH", "BJ"}:
        raise ValueError("instrument_id must be a canonical mainland A-share identifier")
    return code


def _compatible_market_codes(instrument_id: InstrumentId) -> frozenset[str]:
    exchange = str(instrument_id).rsplit(".", maxsplit=1)[-1]
    try:
        return _MARKET_CODES_BY_EXCHANGE[exchange]
    except KeyError as exc:  # pragma: no cover - guarded by _stock_code
        raise ValueError("instrument_id must be a canonical mainland A-share identifier") from exc


def _validate_returned_instrument(
    raw_codes: object,
    *,
    stock_code: str,
    compatible_market_codes: frozenset[str],
) -> None:
    if not isinstance(raw_codes, list) or not raw_codes:
        raise EastmoneyAnnouncementError(
            "announcement codes must be a non-empty array containing the requested instrument"
        )

    matching_markets: list[str] = []
    for index, raw_code in enumerate(cast(list[object], raw_codes)):
        code = _string_mapping(raw_code, f"codes[{index}]")
        returned_stock = _required_text(code.get("stock_code"), "codes.stock_code")
        market_code = _required_text(code.get("market_code"), "codes.market_code")
        if returned_stock == stock_code:
            matching_markets.append(market_code)

    if not matching_markets:
        raise EastmoneyAnnouncementError(
            "announcement codes do not contain the requested instrument"
        )
    unique_markets = set(matching_markets)
    if len(unique_markets) != 1:
        raise EastmoneyAnnouncementError(
            "announcement codes contain ambiguous market markers for the requested instrument"
        )
    if unique_markets.isdisjoint(compatible_market_codes):
        raise EastmoneyAnnouncementError(
            "announcement market marker is incompatible with the requested instrument"
        )


def _columns(raw_columns: object) -> tuple[Mapping[str, str], ...]:
    if not isinstance(raw_columns, list):
        raise EastmoneyAnnouncementError("columns must be an array")
    normalized: list[Mapping[str, str]] = []
    for index, value in enumerate(cast(list[object], raw_columns)):
        column = _string_mapping(value, f"columns[{index}]")
        normalized.append(
            {
                "column_code": _required_text(column.get("column_code"), "column_code"),
                "column_name": _required_text(column.get("column_name"), "column_name"),
            }
        )
    return tuple(normalized)


def _event_classification(
    columns: Sequence[Mapping[str, str]],
    *,
    title: str,
) -> tuple[str, str, str | None]:
    normalized_title = _normalized_title(title)
    if _AMBIGUOUS_OR_NEGATED_TITLE_RE.search(normalized_title) is not None:
        return _UNCLASSIFIED_EVENT_CODE, "unclassified", None

    for column_code in sorted(column["column_code"] for column in columns):
        if mapped := _COLUMN_EVENT_CODES.get(column_code):
            if _REVISED_OR_WITHDRAWN_TITLE_RE.search(normalized_title) is not None and not (
                mapped == "event.repurchase_capital.repurchase_change"
                and _is_explicit_repurchase_revision(normalized_title)
            ):
                return _UNCLASSIFIED_EVENT_CODE, "unclassified", None
            if mapped in DOCUMENT_TEXT_EVENT_CODES and not _is_initial_complete_document_title(
                mapped,
                normalized_title,
            ):
                return _UNCLASSIFIED_EVENT_CODE, "unclassified", None
            return mapped, "column_code", f"eastmoney-column:{column_code}"

    accepted_rules = tuple(
        rule for rule in _TITLE_EVENT_RULES if _title_rule_has_acceptance_evidence(rule)
    )
    return _classify_normalized_title(normalized_title, rules=accepted_rules)


def _classify_normalized_title(
    normalized_title: str,
    *,
    rules: Sequence[_TitleEventRule],
) -> tuple[str, str, str | None]:
    if (
        _AMBIGUOUS_OR_NEGATED_TITLE_RE.search(normalized_title) is not None
        or _REVISED_OR_WITHDRAWN_TITLE_RE.search(normalized_title) is not None
    ):
        return _UNCLASSIFIED_EVENT_CODE, "unclassified", None
    for rule in rules:
        if rule.pattern.search(normalized_title) is None:
            continue
        if rule.excluded is not None and rule.excluded.search(normalized_title) is not None:
            continue
        return rule.event_code, "deterministic_title_rule", rule.rule_id
    return _UNCLASSIFIED_EVENT_CODE, "unclassified", None


def _is_explicit_repurchase_revision(normalized_title: str) -> bool:
    if re.search(r"(?:撤回|撤销|取消|更正|补充)", normalized_title) is not None:
        return False
    return re.search(r"回购股份.*修订|修订.*回购股份", normalized_title) is not None


def _periodic_report_attributes(event_code: str, *, title: str) -> dict[str, str | None]:
    report_type = _PERIODIC_REPORT_TYPES.get(event_code)
    if report_type is None:
        return {}
    normalized = _normalized_title(title)
    year_match = re.search(r"(?<!\d)(20\d{2})年", normalized)
    report_period_end: str | None = None
    if year_match is not None:
        year = int(year_match.group(1))
        if "第一季度" in normalized or "一季度" in normalized:
            report_period_end = date(year, 3, 31).isoformat()
        elif "半年度" in normalized or "半年" in normalized:
            report_period_end = date(year, 6, 30).isoformat()
        elif "第三季度" in normalized or "三季度" in normalized:
            report_period_end = date(year, 9, 30).isoformat()
        elif report_type != "quarterly_report":
            report_period_end = date(year, 12, 31).isoformat()

    if report_type == "earnings_forecast":
        direction_rules = (
            ("turn_profitable", re.compile(r"扭亏(?:为盈)?")),
            ("first_loss", re.compile(r"首亏")),
            ("continued_loss", re.compile(r"续亏")),
            ("continued_profit", re.compile(r"续盈")),
            ("increase", re.compile(r"预增|略增")),
            ("decrease", re.compile(r"预减|略减")),
        )
        direction = next(
            (value for value, pattern in direction_rules if pattern.search(normalized)),
            "unspecified",
        )
        # New observations write only the Strategy Catalog's canonical
        # forecast attributes.  Legacy report_type/forecast_direction aliases
        # are normalized only when immutable old snapshots are read.
        return {
            "forecast_type": report_type,
            "direction": direction,
            "document_version_role": "initial_complete",
            "document_text_scope": "complete_primary_document",
        }
    return {
        "report_type": report_type,
        "stat_date": report_period_end,
        "report_period_quality": "title_exact" if report_period_end else "unresolved",
        "document_version_role": "initial_complete",
        "document_text_scope": "complete_primary_document",
    }


def _is_initial_complete_document_title(event_code: str, normalized_title: str) -> bool:
    if _NON_INITIAL_PERIODIC_DOCUMENT_RE.search(normalized_title) is not None:
        return False
    if event_code == "event.financial_results.annual_report":
        return _INITIAL_COMPLETE_ANNUAL_REPORT_RE.search(normalized_title) is not None
    if event_code == "event.financial_results.semiannual_report":
        return _INITIAL_COMPLETE_SEMIANNUAL_REPORT_RE.search(normalized_title) is not None
    if event_code == "event.financial_results.quarterly_report":
        return _INITIAL_COMPLETE_QUARTERLY_REPORT_RE.search(normalized_title) is not None
    return True


def _validate_unique_initial_periodic_reports(
    observations: Sequence[EventObservation],
) -> None:
    """Reject ambiguous duplicate primary reports for the same reporting period."""

    unique_by_period: dict[tuple[str, str], str] = {}
    duplicate_keys: set[tuple[str, str]] = set()
    for observation in observations:
        if observation.event_code not in {
            "event.financial_results.annual_report",
            "event.financial_results.semiannual_report",
            "event.financial_results.quarterly_report",
        }:
            continue
        stat_date = observation.attributes.get("stat_date")
        if not isinstance(stat_date, str) or not stat_date:
            raise EastmoneyAnnouncementError(
                "initial complete periodic report has no exact reporting period"
            )
        key = (observation.event_code, stat_date)
        previous = unique_by_period.setdefault(key, observation.provider_event_id)
        if previous != observation.provider_event_id:
            duplicate_keys.add(key)
    if duplicate_keys:
        details = ", ".join(
            f"{event_code}@{stat_date}" for event_code, stat_date in sorted(duplicate_keys)
        )
        raise EastmoneyAnnouncementError(
            "multiple initial complete periodic reports share one reporting period: " + details
        )


def _normalized_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", "", normalized)


def _content_url(art_code: str) -> str:
    source_query = urlencode({"art_code": art_code, "client_source": "web", "page_index": 1})
    return f"{ANNOUNCEMENT_CONTENT_URL}?{source_query}"


def _content_pdf_url(
    content: Mapping[str, object],
    *,
    art_code: str,
) -> str | None:
    candidates: list[object] = [
        content.get("attach_url_web"),
        content.get("attach_url"),
    ]
    raw_attachments = content.get("attach_list")
    if raw_attachments is not None:
        if not isinstance(raw_attachments, list):
            raise EastmoneyAnnouncementError("content.data.attach_list must be an array")
        for index, raw_attachment in enumerate(cast(list[object], raw_attachments)):
            attachment = _string_mapping(
                raw_attachment,
                f"content.data.attach_list[{index}]",
            )
            candidates.append(attachment.get("attach_url"))

    for candidate in candidates:
        if candidate is None:
            continue
        value = _optional_text(candidate, "content PDF URL")
        if value is not None:
            return _validated_pdf_url(value, art_code=art_code)
    return None


def _validated_pdf_url(value: str, *, art_code: str) -> str:
    parsed = urlsplit(value)
    decoded_path = unquote(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname != _PDF_HOST
        or not decoded_path.startswith("/pdf/")
        or art_code not in decoded_path
    ):
        raise EastmoneyAnnouncementError(
            "announcement PDF URL is outside the expected Eastmoney document host"
        )
    return value


def _parse_notice_date(value: str) -> date:
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise EastmoneyAnnouncementError(f"notice_date is invalid: {value!r}") from exc


def _parse_optional_provider_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    matched = _PROVIDER_DATETIME.fullmatch(value)
    if matched is None:
        return None
    try:
        parsed = datetime.strptime(matched.group("seconds"), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    milliseconds = matched.group("milliseconds")
    if milliseconds is not None:
        parsed = parsed.replace(microsecond=int(milliseconds) * 1000)
    return parsed.replace(tzinfo=_SHANGHAI)


def _ceil_second(value: datetime) -> datetime:
    if value.microsecond == 0:
        return value
    return (value + timedelta(seconds=1)).replace(microsecond=0)


def _is_plausible_provider_time(value: datetime, notice_day: date) -> bool:
    if value.tzinfo != _SHANGHAI:
        return False
    if value.year < _MIN_CREDIBLE_EITIME_YEAR or notice_day.year < _MIN_CREDIBLE_EITIME_YEAR:
        return False
    return abs((value.date() - notice_day).days) <= _MAX_TIMESTAMP_DISTANCE_DAYS


def _observation_sort_key(observation: EventObservation) -> tuple[datetime, datetime, str]:
    return (
        observation.source_released_at or observation.retrieved_at,
        observation.vendor_first_available_at or observation.retrieved_at,
        observation.provider_event_id,
    )


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EastmoneyAnnouncementError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EastmoneyAnnouncementError(f"{field_name} must be text or null")
    stripped = value.strip()
    return stripped or None


def _optional_raw_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EastmoneyAnnouncementError(f"{field_name} must be text or null")
    return value if value.strip() else None


def _required_non_negative_int(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise EastmoneyAnnouncementError(f"{field_name} must be a non-negative integer")
    return value


def _string_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EastmoneyAnnouncementError(f"{field_name} must be an object")
    raw = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in raw):
        raise EastmoneyAnnouncementError(f"{field_name} keys must be strings")
    return cast(Mapping[str, object], raw)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise EastmoneyAnnouncementError("announcement payload is not JSON serializable") from exc


def _validate_art_code(art_code: str) -> None:
    if _ART_CODE.fullmatch(art_code) is None:
        raise EastmoneyAnnouncementError(f"invalid Eastmoney art_code: {art_code!r}")
