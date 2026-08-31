from __future__ import annotations

import pytest

from ashare_lab.adapters.event_sources import eastmoney
from ashare_lab.adapters.event_sources.announcement_acceptance import (
    TITLE_RULE_ACCEPTANCE_CASES,
)


@pytest.mark.parametrize(
    ("rule_id", "case"),
    sorted(TITLE_RULE_ACCEPTANCE_CASES.items()),
)
def test_every_strict_title_rule_has_positive_and_revision_negative_evidence(
    rule_id: str,
    case: object,
) -> None:
    typed = TITLE_RULE_ACCEPTANCE_CASES[rule_id]
    positive = eastmoney._event_classification((), title=typed.positive_title)
    revised = eastmoney._event_classification((), title=typed.revised_negative_title)
    confusable = tuple(
        eastmoney._event_classification((), title=title)
        for title in typed.confusable_negative_titles
    )

    assert positive == (typed.event_code, "deterministic_title_rule", rule_id)
    assert revised == ("event.announcement.unclassified", "unclassified", None)
    assert typed.confusable_negative_titles
    assert confusable == (("event.announcement.unclassified", "unclassified", None),) * len(
        typed.confusable_negative_titles
    )


def test_acceptance_corpus_and_title_rule_registry_are_exactly_one_to_one() -> None:
    rules = {rule.rule_id: rule.event_code for rule in eastmoney._TITLE_EVENT_RULES}

    assert set(TITLE_RULE_ACCEPTANCE_CASES) == set(rules)
    assert {
        rule_id: case.event_code for rule_id, case in TITLE_RULE_ACCEPTANCE_CASES.items()
    } == rules


@pytest.mark.parametrize(
    "title",
    (
        "某公司:重大项目中标候选人公示",
        "某公司:重大项目预中标公告",
        "某公司:重大项目拟中标提示",
        "某公司:重大项目中标意向公告",
        "某公司:重大项目中标但尚未签订合同的提示公告",
        "某公司:重大项目未正式中标的澄清公告",
        "某财经网站:据网页消息某公司重大项目中标",
    ),
)
def test_major_contract_won_rejects_every_pre_award_or_negated_stage(title: str) -> None:
    assert eastmoney._event_classification((), title=title) == (
        "event.announcement.unclassified",
        "unclassified",
        None,
    )


def test_preparable_codes_are_classifier_collector_intersection_not_catalog_aliases() -> None:
    preparable = eastmoney.eastmoney_preparable_event_codes()

    assert len(preparable) == 68
    assert "event.contracts_orders.major_contract_won" in preparable
    assert "event.macro_policy_industry.license_approval" not in preparable
    assert all(
        eastmoney.eastmoney_event_coverage_contract(code).query_succeeded for code in preparable
    )


def test_removing_acceptance_evidence_removes_title_only_runtime_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rule_id = "cash-dividend-proposal-v1"
    reduced = dict(TITLE_RULE_ACCEPTANCE_CASES)
    reduced.pop(rule_id)
    monkeypatch.setattr(eastmoney, "TITLE_RULE_ACCEPTANCE_CASES", reduced)

    assert (
        "event.dividends_corporate_actions.cash_dividend_proposal"
        not in eastmoney.eastmoney_preparable_event_codes()
    )
    contract = eastmoney.eastmoney_event_coverage_contract(
        "event.dividends_corporate_actions.cash_dividend_proposal"
    )
    assert contract.query_succeeded is False
    assert contract.title_rule_ids == ()
    assert eastmoney._event_classification(
        (),
        title=TITLE_RULE_ACCEPTANCE_CASES[rule_id].positive_title,
    ) == ("event.announcement.unclassified", "unclassified", None)


def test_every_preparable_contract_pins_all_shared_safety_guards() -> None:
    for event_code in eastmoney.eastmoney_preparable_event_codes():
        contract = eastmoney.eastmoney_event_coverage_contract(event_code)
        assert contract.query_succeeded is True
        assert len(contract.classifier_sha256) == 64
        assert contract.coverage_basis in {
            eastmoney.COMPLETE_PROVIDER_COLUMN_COVERAGE_BASIS,
            eastmoney.COMPLETE_DETERMINISTIC_TITLE_COVERAGE_BASIS,
        }
