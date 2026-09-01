from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo

import httpx
import pytest

from ashare_lab.adapters.market_data.eastmoney_corporate_actions import (
    COVERAGE_SCOPE,
    NEGATIVE_PROOF_CATEGORIES,
    SUPPORTED_CATEGORIES,
    EastmoneyCorporateActionError,
    EastmoneyCorporateActionReferenceAdapter,
    EastmoneyLaneCollection,
    _normalize_symbol,
    to_choice_snapshot_payload,
)
from ashare_lab.domain.market_data import CorporateActionKind

SHANGHAI = ZoneInfo("Asia/Shanghai")
CAPTURED_AT = datetime(2025, 1, 10, 9, 0, tzinfo=SHANGHAI)


def test_normalizes_current_bse_920_code_for_public_corporate_action_queries() -> None:
    instrument, digits, prefixed = _normalize_symbol("920001.bj")

    assert str(instrument) == "920001.BJ"
    assert digits == "920001"
    assert prefixed == "BJ920001"


def test_rejects_unproven_legacy_bse_alias_in_corporate_action_queries() -> None:
    with pytest.raises(EastmoneyCorporateActionError, match="supported SH/SZ/BJ"):
        _normalize_symbol("830799.BJ")


def _dividend_row() -> dict[str, object]:
    return {
        "SECUCODE": "300059.SZ",
        "SECURITY_CODE": "300059",
        "REPORT_DATE": "2024-12-31 00:00:00",
        "PLAN_NOTICE_DATE": "2024-12-31 00:00:00",
        "NOTICE_DATE": "2025-01-01 00:00:00",
        "EQUITY_RECORD_DATE": "2025-01-02 00:00:00",
        "EX_DIVIDEND_DATE": "2025-01-03 00:00:00",
        "PRETAX_BONUS_RMB": 1,
        "BONUS_RATIO": None,
        "IT_RATIO": 2,
        "BONUS_IT_RATIO": 2,
        "ASSIGN_PROGRESS": "实施分配",
    }


def _no_distribution_row() -> dict[str, object]:
    return {
        "SECUCODE": "300059.SZ",
        "SECURITY_CODE": "300059",
        "REPORT_DATE": "2024-06-30 00:00:00",
        "NOTICE_DATE": "2024-08-01 00:00:00",
        "EQUITY_RECORD_DATE": None,
        "EX_DIVIDEND_DATE": None,
        "PRETAX_BONUS_RMB": None,
        "BONUS_RATIO": None,
        "IT_RATIO": None,
        "BONUS_IT_RATIO": None,
        "ASSIGN_PROGRESS": "董事会预案",
    }


def _rights_row() -> dict[str, object]:
    return {
        "FINANCE_CODE": "rights-1",
        "SECUCODE": "300059.SZ",
        "SECURITY_CODE": "300059",
        "PLACING_RATIO": 3,
        "ISSUE_PRICE": 5,
        "EQUITY_RECORD_DATE": "2025-01-02 00:00:00",
        "EX_DIVIDEND_DATE": "2025-01-03 00:00:00",
        "PAY_END_DATE": "2025-01-08 00:00:00",
        "LISTING_DATE": "2025-01-09 00:00:00",
        "FIRST_NOTICE_DATE": "2024-12-31 00:00:00",
    }


def _equity_row(
    *,
    changed_at: str = "2025-01-03 00:00:00",
    reason: str = "转增股上市",
) -> dict[str, object]:
    return {
        "SECUCODE": "300059.SZ",
        "SECURITY_CODE": "300059",
        "END_DATE": changed_at,
        "TOTAL_SHARES": 1_200_000,
        "CHANGE_REASON": reason,
    }


def _bonus_payload(*, include_cash_settlement: bool = True) -> dict[str, object]:
    fhyx: list[dict[str, object]] = []
    if include_cash_settlement:
        fhyx.append(
            {
                "SECUCODE": "300059.SZ",
                "SECURITY_CODE": "300059",
                "NOTICE_DATE": "2025-01-01 00:00:00",
                "EQUITY_RECORD_DATE": "2025-01-02 00:00:00",
                "EX_DIVIDEND_DATE": "2025-01-03 00:00:00",
                "PAY_CASH_DATE": "2025-01-04 00:00:00",
                "IMPL_PLAN_PROFILE": "10转2派1元",
                "ASSIGN_PROGRESS": "实施方案",
            }
        )
    return {"fhyx": fhyx, "pgmx": [], "lnfhrz": [], "zfmx": []}


def _implementation_announcement(
    art_code: str = "AN202501011234567890",
    *,
    title: str = "东方财富:东方财富信息股份有限公司2024年度权益分派实施公告",
) -> dict[str, object]:
    return {
        "art_code": art_code,
        "codes": [
            {
                "ann_type": "A,CYB",
                "inner_code": "redacted",
                "market_code": "0",
                "short_name": "东方财富",
                "stock_code": "300059",
            }
        ],
        "columns": [
            {
                "column_code": "001002002001005",
                "column_name": "分配方案实施",
            }
        ],
        "display_time": "2025-01-01 18:00:00:000",
        "eiTime": "2025-01-01 18:00:01:000",
        "language": "0",
        "listing_state": "0",
        "notice_date": "2025-01-01 00:00:00",
        "product_code": "",
        "sort_date": "2025-01-01 12:00:00",
        "source_type": "333",
        "title": title,
        "title_ch": title,
        "title_en": "",
    }


def _implementation_text(
    *,
    record_date: str = "2025年1月2日",
    ex_date: str = "2025年1月3日",
    pay_date: str = "2025年1月4日",
    cash_per_ten: str = "1.00",
) -> str:
    return (
        "东方财富信息股份有限公司2024年度权益分派实施公告。"
        f"向全体股东每10股派发现金股利人民币{cash_per_ten}元（含税）。"
        f"本次权益分派股权登记日为：{record_date}，"
        f"除权除息日为：{ex_date}。"
        f"公司委托中国结算代派的现金红利将于{pay_date}"
        "通过股东托管证券公司直接划入其资金账户。"
    )


def _implementation_content(
    announcement: dict[str, object],
    *,
    text: str | None = None,
) -> dict[str, object]:
    art_code = str(announcement["art_code"])
    title = str(announcement["title"])
    pdf_url = f"https://pdf.dfcfw.com/pdf/H2_{art_code}_1.pdf"
    return {
        "data": {
            "art_code": art_code,
            "attach_list": [{"attach_type": "0", "attach_url": pdf_url, "seq": 1}],
            "attach_url": pdf_url,
            "attach_url_web": pdf_url,
            "eitime": "2025-01-01 18:00:01",
            "notice_content": text if text is not None else _implementation_text(),
            "notice_date": "2025-01-01 00:00:00",
            "notice_title": title,
            "page_size": 1,
            "security": [{"short_name": "东方财富", "stock": "300059"}],
        },
        "success": 1,
    }


def _success_page(
    rows: list[dict[str, object]],
    *,
    pages: int,
    count: int,
) -> dict[str, object]:
    return {
        "version": "fixture",
        "result": {"pages": pages, "data": rows, "count": count},
        "success": True,
        "message": "ok",
        "code": 0,
    }


def _zero_result() -> dict[str, object]:
    return {
        "version": None,
        "result": None,
        "success": False,
        "message": "返回数据为空",
        "code": 9201,
    }


def _transport(
    *,
    dividend_rows: list[dict[str, object]] | None = None,
    rights_rows: list[dict[str, object]] | None = None,
    equity_rows: list[dict[str, object]] | None = None,
    bonus_payload: dict[str, object] | None = None,
    count_override: dict[str, int] | None = None,
    settlement_announcements: list[dict[str, object]] | None = None,
    settlement_contents: dict[str, dict[str, object]] | None = None,
) -> httpx.MockTransport:
    dividends = dividend_rows if dividend_rows is not None else [_dividend_row()]
    rights = rights_rows if rights_rows is not None else []
    equities = (
        equity_rows
        if equity_rows is not None
        else [
            _equity_row(),
            _equity_row(changed_at="2025-01-04 00:00:00", reason="高管股份变动"),
        ]
    )
    bonus = bonus_payload if bonus_payload is not None else _bonus_payload()
    overrides = count_override or {}
    announcements = settlement_announcements or []
    content_by_art_code = settlement_contents or {}

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if request.url.host == "np-anotice-stock.eastmoney.com":
            assert params["stock_list"] == "300059"
            assert params["begin_time"] == "2025-01-01"
            assert params["end_time"] == "2025-01-01"
            assert params["page_index"] == "1"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "list": announcements,
                        "page_index": 1,
                        "page_size": 100,
                        "total_hits": len(announcements),
                    },
                    "error": "",
                    "success": 1,
                },
            )
        if request.url.host == "np-cnotice-stock.eastmoney.com":
            art_code = params["art_code"]
            return httpx.Response(200, json=content_by_art_code[art_code])
        if request.url.path.endswith("/BonusFinancing/PageAjax"):
            assert params["code"] == "SZ300059"
            return httpx.Response(200, json=bonus)
        dataset = params["reportName"]
        rows_by_dataset = {
            "RPT_SHAREBONUS_DET": dividends,
            "RPT_IPO_ALLOTMENT": rights,
            "RPT_F10_EH_EQUITY": equities,
        }
        rows = rows_by_dataset[dataset]
        if not rows:
            return httpx.Response(200, json=_zero_result())
        page_number = int(params["pageNumber"])
        assert params["pageSize"] == "1"
        assert params["filter"] in {
            '(SECURITY_CODE="300059")',
            '(SECUCODE="300059.SZ")',
        }
        page_rows = rows[page_number - 1 : page_number]
        declared_count = overrides.get(dataset, len(rows))
        return httpx.Response(
            200,
            json=_success_page(page_rows, pages=len(rows), count=declared_count),
        )

    return httpx.MockTransport(handler)


def _prepare(transport: httpx.MockTransport):
    with EastmoneyCorporateActionReferenceAdapter(
        transport=transport,
        page_size=1,
    ) as adapter:
        return adapter.prepare(
            symbol="300059.SZ",
            start=date(2025, 1, 1),
            end=date(2025, 1, 5),
            captured_at=CAPTURED_AT,
        )


def test_builds_paginated_cash_share_and_negative_split_proof() -> None:
    result = _prepare(_transport(dividend_rows=[_dividend_row(), _no_distribution_row()]))

    assert [item.action_type for item in result.corporate_actions] == [
        CorporateActionKind.CASH_DIVIDEND,
        CorporateActionKind.SHARE_DISTRIBUTION,
    ]
    cash, shares = result.corporate_actions
    assert cash.gross_cash_per_share == Decimal("0.1")
    assert cash.cash_pay_date == date(2025, 1, 4)
    assert shares.share_multiplier == Decimal("1.2")
    assert shares.share_credit_date == date(2025, 1, 3)
    assert shares.share_sellable_date == date(2025, 1, 3)

    coverage = result.coverage
    assert coverage["status"] == "complete_mixed_mode"
    assert coverage["coverageScope"] == COVERAGE_SCOPE
    assert coverage["positiveCapableCategories"] == list(SUPPORTED_CATEGORIES)
    assert coverage["negativeProofCategories"] == list(NEGATIVE_PROOF_CATEGORIES)
    assert coverage["unsupportedCategories"] == []
    assert coverage["strictEligibleUnderCurrentChoiceValidator"] is True
    assert coverage["strictBlockers"] == []
    categories = coverage["categoryCoverage"]
    assert isinstance(categories, dict)
    assert categories["stock_split"]["categoryMode"] == "complete_negative_proof"
    assert categories["stock_split"]["zeroResult"] is True
    assert categories["reverse_split"]["zeroResult"] is True
    negative = coverage["negativeSplitProof"]
    assert isinstance(negative, dict)
    assert negative["scannedRows"] == 2
    assert negative["recognizedChangeReasons"] == ["转增股上市", "高管股份变动"]

    dividend = result.query_collections[0]
    rights = result.query_collections[1]
    equity = result.query_collections[2]
    assert isinstance(dividend, EastmoneyLaneCollection)
    assert isinstance(rights, EastmoneyLaneCollection)
    assert isinstance(equity, EastmoneyLaneCollection)
    assert len(dividend.pages) == 2
    assert [page.page_number for page in dividend.pages] == [1, 2]
    assert rights.zero_result is True
    assert rights.pages[0].payload == _zero_result()
    assert len(rights.pages[0].raw_wire_sha256) == 64
    assert len(equity.pages) == 2
    assert all(len(page.raw_wire_sha256) == 64 for page in equity.pages)

    payload = to_choice_snapshot_payload(result)
    coverage_payload = cast(dict[str, object], payload["coverage"])
    action_payload = cast(list[dict[str, object]], payload["actions"])
    assert coverage_payload["strictEligibleUnderCurrentChoiceValidator"] is True
    assert [row["action_type"] for row in action_payload] == [
        "cash_dividend",
        "share_distribution",
    ]


def test_normalizes_complete_rights_terms_from_filtered_dataset() -> None:
    result = _prepare(
        _transport(
            dividend_rows=[],
            rights_rows=[_rights_row()],
            equity_rows=[_equity_row(reason="高管股份变动")],
            bonus_payload=_bonus_payload(include_cash_settlement=False),
        )
    )

    assert len(result.corporate_actions) == 1
    rights = result.corporate_actions[0]
    assert rights.action_type is CorporateActionKind.RIGHTS_ISSUE
    assert rights.rights_ratio == Decimal("0.3")
    assert rights.rights_subscription_price == Decimal("5")
    assert rights.rights_payment_deadline == date(2025, 1, 8)
    assert rights.rights_listing_date == date(2025, 1, 9)


@pytest.mark.parametrize(
    "reason",
    ["首发限售股份上市", "网下配售股份上市", "战略配售上市"],
)
def test_negative_split_proof_accepts_known_placement_listing_change(reason: str) -> None:
    result = _prepare(
        _transport(
            dividend_rows=[],
            equity_rows=[_equity_row(reason=reason)],
            bonus_payload=_bonus_payload(include_cash_settlement=False),
        )
    )

    proof = result.coverage["negativeSplitProof"]
    assert isinstance(proof, dict)
    assert proof["recognizedChangeReasons"] == [reason]


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        ("缩股", "reverse-split candidate"),
        ("股份拆细", "stock-split candidate"),
        ("神秘股本调整", "unclassified change reason"),
    ],
)
def test_negative_split_proof_fails_on_positive_or_unknown_reason(
    reason: str,
    message: str,
) -> None:
    with pytest.raises(EastmoneyCorporateActionError, match=message):
        _prepare(
            _transport(
                dividend_rows=[],
                equity_rows=[_equity_row(reason=reason)],
                bonus_payload=_bonus_payload(include_cash_settlement=False),
            )
        )


def test_cash_action_fails_when_limited_f10_enrichment_does_not_contain_pay_date() -> None:
    with pytest.raises(EastmoneyCorporateActionError, match="implementation announcement"):
        _prepare(
            _transport(
                dividend_rows=[_dividend_row()],
                bonus_payload=_bonus_payload(include_cash_settlement=False),
            )
        )


def test_cash_action_uses_frozen_issuer_implementation_announcement_when_f10_is_limited() -> None:
    announcement = _implementation_announcement()
    art_code = str(announcement["art_code"])

    result = _prepare(
        _transport(
            dividend_rows=[_dividend_row()],
            bonus_payload=_bonus_payload(include_cash_settlement=False),
            settlement_announcements=[announcement],
            settlement_contents={
                art_code: _implementation_content(announcement),
            },
        )
    )

    cash = next(
        item
        for item in result.corporate_actions
        if item.action_type is CorporateActionKind.CASH_DIVIDEND
    )
    assert cash.cash_pay_date == date(2025, 1, 4)
    assert art_code in cash.source_url
    assert len(cash.raw_response_sha256) == 64
    settlement = result.query_collections[-1].as_dict()
    assert settlement["lane"] == "cash_settlement_implementation_announcement"
    assert settlement["declaredCount"] == 1
    assert settlement["rowCount"] == 1
    candidate = cast(list[dict[str, object]], settlement["candidateDocuments"])[0]
    assert candidate["providerEventId"] == art_code
    assert candidate["documentSha256"]
    attributes = cast(dict[str, object], candidate["attributes"])
    assert "现金红利将于2025年1月4日" in str(attributes["document_text"])
    assert attributes["document_text_sha256"]
    assert attributes["raw_content_response_sha256"]


@pytest.mark.parametrize(
    "text",
    [
        _implementation_text(record_date="2025年1月5日"),
        _implementation_text(ex_date="2025年1月5日"),
        _implementation_text(cash_per_ten="2.00"),
        _implementation_text(pay_date="2025年1月2日"),
        _implementation_text().replace("直接划入", "另行通知"),
    ],
)
def test_cash_settlement_announcement_must_match_dates_terms_and_explicit_payment(
    text: str,
) -> None:
    announcement = _implementation_announcement()
    art_code = str(announcement["art_code"])

    with pytest.raises(EastmoneyCorporateActionError, match="unambiguous issuer"):
        _prepare(
            _transport(
                dividend_rows=[_dividend_row()],
                bonus_payload=_bonus_payload(include_cash_settlement=False),
                settlement_announcements=[announcement],
                settlement_contents={
                    art_code: _implementation_content(announcement, text=text),
                },
            )
        )


def test_cash_settlement_announcement_fails_closed_when_two_documents_match() -> None:
    first = _implementation_announcement("AN202501011234567890")
    second = _implementation_announcement("AN202501011234567891")

    with pytest.raises(EastmoneyCorporateActionError, match="unambiguous issuer"):
        _prepare(
            _transport(
                dividend_rows=[_dividend_row()],
                bonus_payload=_bonus_payload(include_cash_settlement=False),
                settlement_announcements=[first, second],
                settlement_contents={
                    str(first["art_code"]): _implementation_content(first),
                    str(second["art_code"]): _implementation_content(second),
                },
            )
        )


def test_cash_settlement_title_requires_provider_implementation_column() -> None:
    announcement = _implementation_announcement()
    announcement["columns"] = [{"column_code": "001002008", "column_name": "其他"}]
    art_code = str(announcement["art_code"])

    with pytest.raises(EastmoneyCorporateActionError, match="unambiguous issuer"):
        _prepare(
            _transport(
                dividend_rows=[_dividend_row()],
                bonus_payload=_bonus_payload(include_cash_settlement=False),
                settlement_announcements=[announcement],
                settlement_contents={
                    art_code: _implementation_content(announcement),
                },
            )
        )


def test_cash_settlement_accepts_bounded_profit_distribution_scheme_title_variant() -> None:
    announcement = _implementation_announcement(
        title="东方财富:东方财富信息股份有限公司2024年度利润分配方案实施公告"
    )
    art_code = str(announcement["art_code"])

    result = _prepare(
        _transport(
            dividend_rows=[_dividend_row()],
            bonus_payload=_bonus_payload(include_cash_settlement=False),
            settlement_announcements=[announcement],
            settlement_contents={
                art_code: _implementation_content(announcement),
            },
        )
    )

    cash = next(
        item
        for item in result.corporate_actions
        if item.action_type is CorporateActionKind.CASH_DIVIDEND
    )
    assert cash.cash_pay_date == date(2025, 1, 4)


def test_cash_settlement_binds_the_report_period_not_only_the_action_dates() -> None:
    announcement = _implementation_announcement(
        title="东方财富:东方财富信息股份有限公司2023年度权益分派实施公告"
    )
    art_code = str(announcement["art_code"])

    with pytest.raises(EastmoneyCorporateActionError, match="unambiguous issuer"):
        _prepare(
            _transport(
                dividend_rows=[_dividend_row()],
                bonus_payload=_bonus_payload(include_cash_settlement=False),
                settlement_announcements=[announcement],
                settlement_contents={
                    art_code: _implementation_content(announcement),
                },
            )
        )


def test_does_not_normalize_a_proposal_even_when_it_contains_final_looking_dates() -> None:
    proposal = _dividend_row()
    proposal["ASSIGN_PROGRESS"] = "董事会预案"

    result = _prepare(
        _transport(
            dividend_rows=[proposal],
            equity_rows=[_equity_row(reason="高管股份变动")],
        )
    )

    assert result.corporate_actions == ()


def test_rejects_unknown_dividend_implementation_status() -> None:
    unknown = _dividend_row()
    unknown["ASSIGN_PROGRESS"] = "待确认阶段"

    with pytest.raises(EastmoneyCorporateActionError, match="implementation status"):
        _prepare(_transport(dividend_rows=[unknown]))


def test_negative_split_proof_rejects_empty_capital_structure_history() -> None:
    with pytest.raises(EastmoneyCorporateActionError, match="cannot be empty"):
        _prepare(
            _transport(
                dividend_rows=[],
                equity_rows=[],
                bonus_payload=_bonus_payload(include_cash_settlement=False),
            )
        )


def test_rejects_pagination_count_mismatch() -> None:
    with pytest.raises(EastmoneyCorporateActionError, match="returned 1 rows but declared 2"):
        _prepare(
            _transport(
                dividend_rows=[_no_distribution_row()],
                count_override={"RPT_SHAREBONUS_DET": 2},
            )
        )


def test_rejects_future_interval_before_any_http_request() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        raise AssertionError("network should not be called")

    with (
        EastmoneyCorporateActionReferenceAdapter(transport=httpx.MockTransport(handler)) as adapter,
        pytest.raises(EastmoneyCorporateActionError, match="future"),
    ):
        adapter.prepare(
            symbol="300059.SZ",
            start=date(2025, 1, 1),
            end=date(2025, 1, 11),
            captured_at=CAPTURED_AT,
        )
    assert called is False


def test_rejects_response_for_a_different_security() -> None:
    wrong = _dividend_row()
    wrong["SECURITY_CODE"] = "600000"
    wrong["SECUCODE"] = "600000.SH"
    with pytest.raises(EastmoneyCorporateActionError, match="different security"):
        _prepare(_transport(dividend_rows=[wrong]))


def test_query_page_payloads_are_json_safe() -> None:
    result = _prepare(_transport())
    payload = to_choice_snapshot_payload(result)
    encoded = __import__("json").dumps(payload, sort_keys=True)
    assert "rawWireSha256" in encoded
    assert "rawPayload" in encoded
    assert isinstance(payload["queryCollections"], list)
    assert payload["queryCollections"]
