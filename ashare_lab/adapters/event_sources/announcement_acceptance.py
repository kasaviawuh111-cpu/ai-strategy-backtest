"""Machine-checkable acceptance corpus for strict announcement classifiers.

This is part of the adapter contract, not a synonym dictionary.  A title rule
is eligible for on-demand preparation only when it has an explicit positive
issuer-announcement example and a revision-shaped confusable negative here.
Tests execute every row against the real classifier.  Adding a Catalog event or
a regex without adding and passing one of these cases cannot expand runtime
capability.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AnnouncementTitleAcceptanceCase:
    event_code: str
    positive_title: str
    confusable_negative_titles: tuple[str, ...] = ()

    @property
    def revised_negative_title(self) -> str:
        return f"{self.positive_title}（更正公告）"


_POSITIVE_TITLE_RULE_ACCEPTANCE_CASES = {
    "cash-dividend-ex-date-v1": AnnouncementTitleAcceptanceCase(
        "event.dividends_corporate_actions.cash_dividend_ex_date",
        "某公司:2025年度现金红利发放日公告",
    ),
    "cash-dividend-approved-v1": AnnouncementTitleAcceptanceCase(
        "event.dividends_corporate_actions.cash_dividend_approved",
        "某公司:现金分红方案获股东大会审议通过的公告",
    ),
    "cash-dividend-proposal-v1": AnnouncementTitleAcceptanceCase(
        "event.dividends_corporate_actions.cash_dividend_proposal",
        "某公司:2025年度现金分红预案公告",
    ),
    "stock-dividend-ex-date-v1": AnnouncementTitleAcceptanceCase(
        "event.dividends_corporate_actions.stock_dividend_ex_date",
        "某公司:2025年度送红股实施公告",
    ),
    "stock-dividend-proposal-v1": AnnouncementTitleAcceptanceCase(
        "event.dividends_corporate_actions.stock_dividend_proposal",
        "某公司:2025年度送股方案公告",
    ),
    "capitalization-issue-v1": AnnouncementTitleAcceptanceCase(
        "event.dividends_corporate_actions.capitalization_issue",
        "某公司:资本公积金转增股本实施公告",
    ),
    "rights-issue-v1": AnnouncementTitleAcceptanceCase(
        "event.dividends_corporate_actions.rights_issue",
        "某公司:配股发行公告",
    ),
    "unlock-schedule-change-v1": AnnouncementTitleAcceptanceCase(
        "event.restricted_shares_pledges.unlock_schedule_change",
        "某公司:限售股份上市流通日期调整公告",
    ),
    "restricted-shares-unlock-v1": AnnouncementTitleAcceptanceCase(
        "event.restricted_shares_pledges.restricted_shares_unlock",
        "某公司:首次公开发行前已发行股份上市流通公告",
    ),
    "actual-controller-change-v1": AnnouncementTitleAcceptanceCase(
        "event.shareholder_holdings.actual_controller_change",
        "某公司:实际控制人发生变更的公告",
    ),
    "ownership-below-five-percent-v1": AnnouncementTitleAcceptanceCase(
        "event.shareholder_holdings.ownership_below_five_percent",
        "某公司:股东持股比例降至5%以下的权益变动公告",
    ),
    "ownership-reaches-five-percent-v1": AnnouncementTitleAcceptanceCase(
        "event.shareholder_holdings.ownership_reaches_five_percent",
        "某公司:股东持股比例达到5%的权益变动公告",
    ),
    "executive-decrease-v1": AnnouncementTitleAcceptanceCase(
        "event.shareholder_holdings.executive_decrease",
        "某公司:董监高减持股份结果公告",
    ),
    "executive-increase-v1": AnnouncementTitleAcceptanceCase(
        "event.shareholder_holdings.executive_increase",
        "某公司:董监高增持股份结果公告",
    ),
    "major-holder-decrease-progress-v1": AnnouncementTitleAcceptanceCase(
        "event.shareholder_holdings.major_holder_decrease_progress",
        "某公司:控股股东减持计划实施进展公告",
    ),
    "major-holder-decrease-plan-v1": AnnouncementTitleAcceptanceCase(
        "event.shareholder_holdings.major_holder_decrease_plan",
        "某公司:控股股东减持股份预披露公告",
    ),
    "major-holder-increase-progress-v1": AnnouncementTitleAcceptanceCase(
        "event.shareholder_holdings.major_holder_increase_progress",
        "某公司:控股股东增持计划实施进展公告",
    ),
    "major-holder-increase-plan-v1": AnnouncementTitleAcceptanceCase(
        "event.shareholder_holdings.major_holder_increase_plan",
        "某公司:控股股东拟增持股份计划公告",
    ),
    "repurchase-termination-v1": AnnouncementTitleAcceptanceCase(
        "event.repurchase_capital.repurchase_termination",
        "某公司:关于终止回购公司股份的公告",
    ),
    "repurchase-cancellation-v1": AnnouncementTitleAcceptanceCase(
        "event.repurchase_capital.repurchase_cancellation",
        "某公司:关于注销已回购股份的公告",
    ),
    "repurchase-change-v1": AnnouncementTitleAcceptanceCase(
        "event.repurchase_capital.repurchase_change",
        "某公司:关于调整回购股份用途的公告",
    ),
    "repurchase-first-execution-v1": AnnouncementTitleAcceptanceCase(
        "event.repurchase_capital.repurchase_first_execution",
        "某公司:关于首次回购公司股份的公告",
    ),
    "repurchase-completion-v1": AnnouncementTitleAcceptanceCase(
        "event.repurchase_capital.repurchase_completion",
        "某公司:关于回购股份实施完成的公告",
    ),
    "repurchase-progress-v1": AnnouncementTitleAcceptanceCase(
        "event.repurchase_capital.repurchase_progress",
        "某公司:关于回购股份进展的公告",
    ),
    "repurchase-approved-v1": AnnouncementTitleAcceptanceCase(
        "event.repurchase_capital.repurchase_approved",
        "某公司:股东大会审议通过回购股份方案的公告",
    ),
    "repurchase-proposal-v1": AnnouncementTitleAcceptanceCase(
        "event.repurchase_capital.repurchase_proposal",
        "某公司:关于回购公司股份方案的公告",
    ),
    "equity-incentive-grant-v1": AnnouncementTitleAcceptanceCase(
        "event.governance_personnel.equity_incentive_grant",
        "某公司:限制性股票激励计划首次授予公告",
    ),
    "equity-incentive-plan-v1": AnnouncementTitleAcceptanceCase(
        "event.governance_personnel.equity_incentive_plan",
        "某公司:限制性股票激励计划草案公告",
    ),
    "public-censure-v1": AnnouncementTitleAcceptanceCase(
        "event.regulation_risk.public_censure",
        "某公司:关于收到公开谴责决定书的公告",
    ),
    "disciplinary-action-v1": AnnouncementTitleAcceptanceCase(
        "event.regulation_risk.disciplinary_action",
        "某公司:关于收到纪律处分决定书的公告",
    ),
    "administrative-penalty-v1": AnnouncementTitleAcceptanceCase(
        "event.regulation_risk.administrative_penalty",
        "某公司:关于收到行政处罚决定书的公告",
    ),
    "investigation-opened-v1": AnnouncementTitleAcceptanceCase(
        "event.regulation_risk.investigation_opened",
        "某公司:关于收到立案告知书的公告",
    ),
    "information-disclosure-violation-v1": AnnouncementTitleAcceptanceCase(
        "event.regulation_risk.information_disclosure_violation",
        "某公司:关于信息披露违法违规认定的公告",
    ),
    "litigation-progress-v1": AnnouncementTitleAcceptanceCase(
        "event.litigation_credit.litigation_progress",
        "某公司:关于重大诉讼进展的公告",
    ),
    "major-litigation-v1": AnnouncementTitleAcceptanceCase(
        "event.litigation_credit.major_litigation",
        "某公司:关于新增重大诉讼的公告",
    ),
    "arbitration-v1": AnnouncementTitleAcceptanceCase(
        "event.litigation_credit.arbitration",
        "某公司:关于涉及重大仲裁事项的公告",
    ),
    "debt-default-v1": AnnouncementTitleAcceptanceCase(
        "event.litigation_credit.debt_default",
        "某公司:关于债券未能按期兑付本息的公告",
    ),
    "credit-rating-downgrade-v1": AnnouncementTitleAcceptanceCase(
        "event.litigation_credit.credit_rating_downgrade",
        "某公司:关于主体信用评级下调的公告",
    ),
    "bankruptcy-reorganization-v1": AnnouncementTitleAcceptanceCase(
        "event.litigation_credit.bankruptcy_reorganization",
        "某公司:关于法院受理破产重整申请的公告",
    ),
    "chairman-change-v1": AnnouncementTitleAcceptanceCase(
        "event.governance_personnel.chairman_change",
        "某公司:关于董事长辞职暨选举新任董事长的公告",
    ),
    "ceo-change-v1": AnnouncementTitleAcceptanceCase(
        "event.governance_personnel.ceo_change",
        "某公司:关于总经理辞职及聘任新任总经理的公告",
    ),
    "cfo-change-v1": AnnouncementTitleAcceptanceCase(
        "event.governance_personnel.cfo_change",
        "某公司:关于财务总监辞职及聘任的公告",
    ),
    "board-secretary-change-v1": AnnouncementTitleAcceptanceCase(
        "event.governance_personnel.board_secretary_change",
        "某公司:关于董事会秘书辞职及聘任的公告",
    ),
    "director-resignation-v1": AnnouncementTitleAcceptanceCase(
        "event.governance_personnel.director_resignation",
        "某公司:关于独立董事辞职的公告",
    ),
    "supervisor-resignation-v1": AnnouncementTitleAcceptanceCase(
        "event.governance_personnel.supervisor_resignation",
        "某公司:关于监事辞职的公告",
    ),
    "restructuring-terminated-v1": AnnouncementTitleAcceptanceCase(
        "event.m_and_a_restructuring.restructuring_terminated",
        "某公司:关于终止重大资产重组事项的公告",
    ),
    "restructuring-completed-v1": AnnouncementTitleAcceptanceCase(
        "event.m_and_a_restructuring.restructuring_completed",
        "某公司:重大资产重组实施完成公告",
    ),
    "restructuring-regulatory-approval-v1": AnnouncementTitleAcceptanceCase(
        "event.m_and_a_restructuring.restructuring_regulatory_approval",
        "某公司:发行股份购买资产事项获得证监会同意注册的公告",
    ),
    "restructuring-shareholder-approval-v1": AnnouncementTitleAcceptanceCase(
        "event.m_and_a_restructuring.restructuring_approved_shareholders",
        "某公司:股东大会审议通过发行股份购买资产事项的公告",
    ),
    "restructuring-board-approval-v1": AnnouncementTitleAcceptanceCase(
        "event.m_and_a_restructuring.restructuring_approved_board",
        "某公司:董事会审议通过发行股份购买资产事项的公告",
    ),
    "restructuring-resumption-v1": AnnouncementTitleAcceptanceCase(
        "event.m_and_a_restructuring.restructuring_resumption",
        "某公司:重大资产重组事项复牌公告",
    ),
    "restructuring-suspension-v1": AnnouncementTitleAcceptanceCase(
        "event.m_and_a_restructuring.restructuring_suspension",
        "某公司:重大资产重组事项停牌公告",
    ),
    "spin-off-listing-v1": AnnouncementTitleAcceptanceCase(
        "event.m_and_a_restructuring.spin_off_listing",
        "某公司:关于分拆子公司上市的公告",
    ),
    "asset-sale-plan-v1": AnnouncementTitleAcceptanceCase(
        "event.m_and_a_restructuring.asset_sale_plan",
        "某公司:重大资产出售预案公告",
    ),
    "acquisition-plan-v1": AnnouncementTitleAcceptanceCase(
        "event.m_and_a_restructuring.acquisition_plan",
        "某公司:发行股份购买资产预案公告",
    ),
    "major-contract-terminated-v1": AnnouncementTitleAcceptanceCase(
        "event.contracts_orders.contract_terminated",
        "某公司:关于终止重大经营合同的公告",
    ),
    "major-contract-changed-v1": AnnouncementTitleAcceptanceCase(
        "event.contracts_orders.contract_changed",
        "某公司:关于变更重大经营合同的公告",
    ),
    "major-contract-completed-v1": AnnouncementTitleAcceptanceCase(
        "event.contracts_orders.contract_completed",
        "某公司:关于重大经营合同履行完毕的公告",
    ),
    "major-contract-progress-v1": AnnouncementTitleAcceptanceCase(
        "event.contracts_orders.contract_progress",
        "某公司:关于重大经营合同履行进展的公告",
    ),
    "major-contract-won-v1": AnnouncementTitleAcceptanceCase(
        "event.contracts_orders.major_contract_won",
        "某公司:关于重大项目正式中标的公告",
    ),
    "major-contract-signed-v1": AnnouncementTitleAcceptanceCase(
        "event.contracts_orders.major_contract_signed",
        "某公司:关于签订重大经营合同的公告",
    ),
    "framework-agreement-v1": AnnouncementTitleAcceptanceCase(
        "event.contracts_orders.framework_agreement",
        "某公司:关于签署战略合作框架协议的公告",
    ),
    "major-order-received-v1": AnnouncementTitleAcceptanceCase(
        "event.contracts_orders.purchase_order_received",
        "某公司:关于收到重大订单的公告",
    ),
}


def _keyword_rich_confusable_negative(positive_title: str) -> str:
    """Keep the positive keywords while making the title explicitly non-factual."""

    return f"某公司:关于市场传闻“{positive_title}”不实的澄清公告"


_MAJOR_CONTRACT_WON_CONFUSABLE_NEGATIVES = (
    "某公司:重大项目中标候选人公示",
    "某公司:重大项目预中标公告",
    "某公司:重大项目拟中标提示",
    "某公司:重大项目入围通知",
    "某公司:重大项目未正式中标的公告",
    "某公司:关于重大项目中标传闻不实的澄清公告",
    "某财经网站:据网页消息某公司重大项目中标",
    "某公司:重大项目中标但尚未签订合同的提示公告",
)

_SPECIAL_CONFUSABLE_NEGATIVES = {
    "executive-decrease-v1": ("某公司:关于高级管理人员减持股份的预披露公告",),
    "executive-increase-v1": ("某公司:关于高级管理人员拟增持股份计划的公告",),
    "equity-incentive-grant-v1": (
        "某公司:限制性股票激励计划首次授予部分第二个归属期归属条件成就的公告",
    ),
}


# Export fully materialized evidence cases.  Every rule carries a keyword-rich
# confusable negative in addition to its revision-shaped negative.  The major
# award lifecycle has a stricter, explicit stage matrix because a candidate,
# pre-award, shortlist or third-party web claim is not an issuer award event.
TITLE_RULE_ACCEPTANCE_CASES = {
    rule_id: AnnouncementTitleAcceptanceCase(
        event_code=case.event_code,
        positive_title=case.positive_title,
        confusable_negative_titles=(
            _MAJOR_CONTRACT_WON_CONFUSABLE_NEGATIVES
            if rule_id == "major-contract-won-v1"
            else _SPECIAL_CONFUSABLE_NEGATIVES.get(
                rule_id,
                (_keyword_rich_confusable_negative(case.positive_title),),
            )
        ),
    )
    for rule_id, case in _POSITIVE_TITLE_RULE_ACCEPTANCE_CASES.items()
}


__all__ = ["TITLE_RULE_ACCEPTANCE_CASES", "AnnouncementTitleAcceptanceCase"]
