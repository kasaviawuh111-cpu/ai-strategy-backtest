from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import pytest

from ashare_lab.adapters.event_sources.document_text import (
    AnnouncementDocumentTextExtractor,
    ExtractedDocumentText,
    ExtractedTextPage,
)
from ashare_lab.adapters.event_sources.eastmoney import (
    _TITLE_EVENT_RULES,
    ANNOUNCEMENT_CLASSIFIER_VERSION,
    ANNOUNCEMENT_CONTENT_URL,
    ANNOUNCEMENT_LIST_URL,
    COMPLETE_PROVIDER_COLUMN_COVERAGE_BASIS,
    EastmoneyAnnouncementError,
    EastmoneyAnnouncementSource,
    eastmoney_event_coverage_contract,
    eastmoney_provider_column_codes,
)
from ashare_lab.adapters.host_http import HostThrottle
from ashare_lab.domain.events import market_available_at
from ashare_lab.domain.events.catalog import PUBLIC_ANNOUNCEMENT_EVENT_CODES
from ashare_lab.domain.market_data import TimeQuality
from ashare_lab.domain.shared import InstrumentId
from scripts.prepare_event_snapshot import DEFAULT_EVENT_CODES

SHANGHAI = ZoneInfo("Asia/Shanghai")
INSTRUMENT = InstrumentId("300059.SZ")
RETRIEVED_AT = datetime(2026, 8, 29, 12, 0, tzinfo=SHANGHAI)
_EXPECTED_DEFAULT_EVENT_CODES = (
    "event.financial_results.earnings_forecast_published",
    "event.financial_results.earnings_flash_report",
    "event.financial_results.annual_report",
    "event.financial_results.semiannual_report",
    "event.financial_results.quarterly_report",
    "event.repurchase_capital.repurchase_change",
)
_EXPECTED_DEFAULT_EVENT_COLUMNS = {
    "event.financial_results.earnings_forecast_published": ("001002004001",),
    "event.financial_results.earnings_flash_report": ("001002004002",),
    "event.financial_results.annual_report": ("001001001001001",),
    "event.financial_results.semiannual_report": ("001001001002001",),
    "event.financial_results.quarterly_report": (
        "001001001003001",
        "001001001004001",
    ),
    "event.repurchase_capital.repurchase_change": ("001002007003009001002",),
}


def test_title_rules_match_public_announcement_release_and_have_unique_ids() -> None:
    rule_ids = [rule.rule_id for rule in _TITLE_EVENT_RULES]
    expected_event_codes = set(PUBLIC_ANNOUNCEMENT_EVENT_CODES) | {
        "event.contracts_orders.major_contract_won"
    }

    assert len(rule_ids) == len(set(rule_ids))
    assert {rule.event_code for rule in _TITLE_EVENT_RULES} == expected_event_codes


def test_only_validated_title_rule_event_claims_provider_column_interval_recall() -> None:
    title_event_codes = {rule.event_code for rule in _TITLE_EVENT_RULES}
    provider_backed_title_events = {
        "event.repurchase_capital.repurchase_change": ("001002007003009001002",),
    }

    assert title_event_codes & _EXPECTED_DEFAULT_EVENT_COLUMNS.keys() == set(
        provider_backed_title_events
    )
    assert {
        code: eastmoney_provider_column_codes(code)
        for code in title_event_codes
        if eastmoney_provider_column_codes(code)
    } == provider_backed_title_events
    assert {
        code: eastmoney_provider_column_codes(code) for code in _EXPECTED_DEFAULT_EVENT_COLUMNS
    } == _EXPECTED_DEFAULT_EVENT_COLUMNS


def _announcement(
    art_code: str,
    *,
    title: str = "东方财富:测试公告",
    display_time: str = "",
    ei_time: str = "2024-03-14 20:57:33:000",
    notice_date: str = "2024-03-15 00:00:00",
    column_code: str = "001002008",
    column_name: str = "其他",
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
        "columns": [{"column_code": column_code, "column_name": column_name}],
        "display_time": display_time,
        "eiTime": ei_time,
        "language": "0",
        "listing_state": "0",
        "notice_date": notice_date,
        "product_code": "",
        "sort_date": notice_date[:10] + " 12:00:00",
        "source_type": "333",
        "title": title,
        "title_ch": title,
        "title_en": "",
    }


def _body(
    items: list[dict[str, object]],
    *,
    page_index: int = 1,
    page_size: int = 100,
    total_hits: int | None = None,
) -> bytes:
    payload = {
        "data": {
            "list": items,
            "page_index": page_index,
            "page_size": page_size,
            "total_hits": len(items) if total_hits is None else total_hits,
        },
        "error": "",
        "success": 1,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _content_body(
    art_code: str,
    *,
    notice_content: str = "\n公告正文（测试快照）\n",
    eitime: str = "",
    notice_title: str = "东方财富:测试公告",
    attach_url: str | None = None,
) -> bytes:
    pdf_url = attach_url or f"https://pdf.dfcfw.com/pdf/H2_{art_code}_1.pdf"
    payload = {
        "data": {
            "art_code": art_code,
            "attach_list": [{"attach_type": "0", "attach_url": pdf_url, "seq": 1}],
            "attach_url": pdf_url,
            "attach_url_web": pdf_url,
            "eitime": eitime,
            "notice_content": notice_content,
            "notice_date": "2024-03-15 00:00:00",
            "notice_title": notice_title,
            "page_size": 1,
            "security": [{"short_name": "东方财富", "stock": "300059"}],
        },
        "success": 1,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _transport_for_bodies(
    bodies: dict[int, bytes],
    requests: list[httpx.Request] | None = None,
    *,
    content_bodies: dict[str, bytes] | None = None,
    content_status: int = 200,
    pdf_bodies: dict[str, bytes] | None = None,
) -> httpx.MockTransport:
    titles_by_art_code: dict[str, str] = {}
    for body in bodies.values():
        decoded = json.loads(body)
        for raw_item in decoded["data"]["list"]:
            titles_by_art_code[str(raw_item["art_code"])] = str(raw_item["title"])

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if request.url.host == "np-anotice-stock.eastmoney.com":
            page = int(request.url.params["page_index"])
            return httpx.Response(
                200,
                content=bodies[page],
                headers={"Content-Type": "application/json"},
                request=request,
            )
        if request.url.host == "np-cnotice-stock.eastmoney.com":
            art_code = request.url.params["art_code"]
            content = (content_bodies or {}).get(
                art_code,
                _content_body(
                    art_code,
                    notice_title=titles_by_art_code.get(art_code, "东方财富:测试公告"),
                ),
            )
            return httpx.Response(
                content_status,
                content=content,
                headers={"Content-Type": "application/json"},
                request=request,
            )
        if request.url.host == "pdf.dfcfw.com":
            art_code = request.url.path.split("H2_", maxsplit=1)[-1].split("_1.pdf", maxsplit=1)[0]
            content = (pdf_bodies or {}).get(art_code, b"")
            return httpx.Response(
                200 if content else 404,
                content=content,
                headers={"Content-Type": "application/pdf"},
                request=request,
            )
        return httpx.Response(500, request=request)

    return httpx.MockTransport(handler)


def _fetch_one(
    item: dict[str, object],
    *,
    retrieved_at: datetime = RETRIEVED_AT,
    content_body: bytes | None = None,
    content_status: int = 200,
    pdf_body: bytes | None = None,
    extract_document_text: bool = False,
    document_text_extractor: AnnouncementDocumentTextExtractor | None = None,
):
    body = _body([item])
    requests: list[httpx.Request] = []
    art_code = str(item["art_code"])
    with EastmoneyAnnouncementSource(
        transport=_transport_for_bodies(
            {1: body},
            requests,
            content_bodies={art_code: content_body} if content_body is not None else None,
            content_status=content_status,
            pdf_bodies={art_code: pdf_body} if pdf_body is not None else None,
        ),
        extract_document_text=extract_document_text,
        document_text_extractor=document_text_extractor,
    ) as source:
        observations = source.fetch(
            instrument_id=INSTRUMENT,
            start=date.fromisoformat(str(item["notice_date"])[:10]),
            end=date.fromisoformat(str(item["notice_date"])[:10]),
            retrieved_at=retrieved_at,
        )
    return observations[0], body, requests


def test_real_seconds_sample_uses_announcement_service_and_preserves_provenance() -> None:
    item = _announcement(
        "AN202608271828551090",
        title="东方财富:东方财富信息股份有限公司关于完成工商变更登记的公告",
        display_time="2026-08-27 18:34:11:530",
        ei_time="2026-08-27 18:35:16:000",
        notice_date="2026-08-27 00:00:00",
        column_code="001002005001007",
        column_name="公司注册资本变更",
    )

    observation, raw_body, requests = _fetch_one(item)

    assert len(requests) == 2
    request = next(
        request for request in requests if request.url.host == "np-anotice-stock.eastmoney.com"
    )
    assert str(request.url).startswith(ANNOUNCEMENT_LIST_URL)
    assert "push2" not in request.url.host
    assert request.url.params["stock_list"] == "300059"
    assert request.url.params["begin_time"] == "2026-08-27"
    assert request.url.params["end_time"] == "2026-08-27"

    assert observation.provider == "eastmoney"
    assert observation.provider_event_id == "AN202608271828551090"
    assert observation.instrument_id == INSTRUMENT
    assert observation.source_released_at == datetime(
        2026,
        8,
        27,
        18,
        34,
        12,
        tzinfo=SHANGHAI,
    )
    assert observation.vendor_first_available_at == datetime(
        2026,
        8,
        27,
        18,
        35,
        16,
        tzinfo=SHANGHAI,
    )
    vendor_first_available_at = observation.vendor_first_available_at
    assert vendor_first_available_at is not None
    assert vendor_first_available_at.tzinfo == SHANGHAI
    assert vendor_first_available_at.microsecond == 0
    assert observation.retrieved_at == RETRIEVED_AT
    assert observation.time_quality is TimeQuality.VENDOR_OBSERVED
    assert observation.validation_status == "validated"
    expected_hash = hashlib.sha256(raw_body).hexdigest()
    assert observation.raw_response_sha256 == expected_hash
    assert observation.attributes["raw_response_sha256"] == expected_hash
    assert observation.attributes["source_event_id"] == observation.provider_event_id
    assert str(observation.attributes["source_url"]).startswith(ANNOUNCEMENT_CONTENT_URL)
    assert observation.attributes["ingested_at"] == RETRIEVED_AT.isoformat()
    assert observation.attributes["validation_status"] == "validated"
    assert observation.attributes["timestamp_precision"] == "second"
    assert observation.attributes["raw_display_time"] == "2026-08-27 18:34:11:530"
    assert observation.attributes["raw_ei_time"] == "2026-08-27 18:35:16:000"
    assert str(observation.document_url).startswith(ANNOUNCEMENT_CONTENT_URL)
    assert (
        observation.document_sha256
        == hashlib.sha256("\n公告正文（测试快照）\n".encode()).hexdigest()
    )
    assert observation.attributes["document_hash_basis"] == "notice_content_utf8"
    assert observation.attributes["raw_content_response_sha256"]
    assert observation.attributes["conservative_available_at"] == ("2026-08-27T18:35:16+08:00")
    raw_payload = json.loads(str(observation.attributes["raw_payload_json"]))
    assert raw_payload["columns"] == item["columns"]
    raw_content = json.loads(str(observation.attributes["raw_content_payload_json"]))
    assert raw_content["notice_content"] == "\n公告正文（测试快照）\n"


def test_missing_display_time_uses_valid_vendor_seconds_without_inventing_source_time() -> None:
    item = _announcement(
        "AN202403141626765931",
        title="东方财富:2023年年度报告",
        display_time="",
        ei_time="2024-03-14 20:57:33:000",
        notice_date="2024-03-15 00:00:00",
        column_code="001001001001001",
        column_name="年度报告全文",
    )

    observation, _, _ = _fetch_one(item)

    assert observation.event_code == "event.financial_results.annual_report"
    assert observation.attributes["source"] == "eastmoney"
    assert observation.source_released_at is None
    assert observation.vendor_first_available_at == datetime(
        2024,
        3,
        14,
        20,
        57,
        33,
        tzinfo=SHANGHAI,
    )
    assert observation.time_quality is TimeQuality.VENDOR_OBSERVED
    assert observation.validation_status == "validated"
    assert json.loads(str(observation.attributes["raw_columns_json"])) == item["columns"]
    assert observation.attributes["report_type"] == "annual_report"
    assert observation.attributes["stat_date"] == "2023-12-31"
    assert observation.attributes["report_period_quality"] == "title_exact"


def test_forecast_writes_only_canonical_catalog_attributes() -> None:
    item = _announcement(
        "AN202507141626765931",
        title="东方财富:2025年半年度业绩预增公告",
        column_code="001002004001",
        column_name="业绩预告",
    )

    observation, _, _ = _fetch_one(item)

    assert observation.event_code == "event.financial_results.earnings_forecast_published"
    assert observation.attributes["source"] == "eastmoney"
    assert observation.attributes["forecast_type"] == "earnings_forecast"
    assert observation.attributes["direction"] == "increase"
    assert "report_type" not in observation.attributes
    assert "forecast_direction" not in observation.attributes
    assert "stat_date" not in observation.attributes


@pytest.mark.parametrize(
    ("notice_date", "ei_time"),
    [
        ("2010-12-10 00:00:00", "2013-03-14 15:32:27:000"),
        ("2024-03-15 00:00:00", "2026-08-27 18:35:16:000"),
    ],
)
def test_pre_2017_or_abnormal_year_eitime_is_rejected_and_date_only(
    notice_date: str,
    ei_time: str,
) -> None:
    item = _announcement(
        "AN201203010004712554",
        display_time="",
        ei_time=ei_time,
        notice_date=notice_date,
    )

    observation, _, _ = _fetch_one(item)

    notice_day = date.fromisoformat(notice_date[:10])
    assert observation.source_released_at == datetime.combine(
        notice_day,
        datetime.min.time().replace(hour=15),
        tzinfo=SHANGHAI,
    )
    assert observation.vendor_first_available_at is None
    assert observation.time_quality is TimeQuality.DATE_ONLY_CONSERVATIVE
    assert observation.validation_status == "rejected"
    assert observation.attributes["timestamp_precision"] == "date"
    assert observation.attributes["raw_ei_time"] == ei_time


def test_missing_eitime_is_unverified_and_cannot_be_promoted_as_demo_canonical() -> None:
    item = _announcement(
        "AN202403141626765950",
        display_time="",
        ei_time="",
        notice_date="2024-03-15 00:00:00",
    )

    observation, _, _ = _fetch_one(item)

    assert observation.vendor_first_available_at is None
    assert observation.time_quality is TimeQuality.DATE_ONLY_CONSERVATIVE
    assert observation.validation_status == "unverified"
    assert observation.attributes["timestamp_precision"] == "date"
    assert observation.validation_status != "validated"


@pytest.mark.parametrize(
    ("codes", "message"),
    [
        (
            [{"stock_code": "600519", "market_code": "1"}],
            "do not contain the requested instrument",
        ),
        (
            [{"stock_code": "300059", "market_code": "1"}],
            "market marker is incompatible",
        ),
        (
            [
                {"stock_code": "300059", "market_code": "0"},
                {"stock_code": "300059", "market_code": "1"},
            ],
            "ambiguous market markers",
        ),
    ],
)
def test_list_record_must_unambiguously_belong_to_requested_instrument(
    codes: list[dict[str, str]],
    message: str,
) -> None:
    item = _announcement("AN202403141626765950")
    item["codes"] = codes
    page = _body([item])

    with (
        EastmoneyAnnouncementSource(
            transport=_transport_for_bodies({1: page}),
        ) as source,
        pytest.raises(EastmoneyAnnouncementError, match=message),
    ):
        source.fetch(
            instrument_id=INSTRUMENT,
            start=date(2024, 3, 15),
            end=date(2024, 3, 15),
            retrieved_at=RETRIEVED_AT,
        )


@pytest.mark.parametrize(
    ("instrument_id", "stock_code", "market_code"),
    [
        (InstrumentId("600519.SH"), "600519", "1"),
        (InstrumentId("920002.BJ"), "920002", "0"),
    ],
)
def test_list_record_accepts_compatible_exchange_market_markers(
    instrument_id: InstrumentId,
    stock_code: str,
    market_code: str,
) -> None:
    item = _announcement("AN202403141626765950")
    item["codes"] = [{"stock_code": stock_code, "market_code": market_code}]
    page = _body([item])

    with EastmoneyAnnouncementSource(
        transport=_transport_for_bodies({1: page}),
    ) as source:
        observations = source.fetch(
            instrument_id=instrument_id,
            start=date(2024, 3, 15),
            end=date(2024, 3, 15),
            retrieved_at=RETRIEVED_AT,
        )

    assert observations[0].instrument_id == instrument_id


def test_duplicate_pages_cannot_satisfy_total_hits_with_fewer_unique_art_codes() -> None:
    newest = _announcement(
        "AN202403151626765999",
        ei_time="2024-03-15 18:00:00:000",
        notice_date="2024-03-15 00:00:00",
    )
    middle = _announcement(
        "AN202403141626765998",
        ei_time="2024-03-14 18:00:00:000",
        notice_date="2024-03-14 00:00:00",
    )
    oldest = _announcement(
        "AN202403131626765997",
        ei_time="2024-03-13 18:00:00:000",
        notice_date="2024-03-13 00:00:00",
    )
    page_one = _body([newest, middle], page_index=1, page_size=2, total_hits=4)
    changed_duplicate = {**middle, "source_type": "duplicate-page-value"}
    page_two = _body(
        [changed_duplicate, oldest],
        page_index=2,
        page_size=2,
        total_hits=4,
    )
    requests: list[httpx.Request] = []

    with (
        EastmoneyAnnouncementSource(
            transport=_transport_for_bodies({1: page_one, 2: page_two}, requests),
            page_size=2,
            max_pages=2,
        ) as source,
        pytest.raises(EastmoneyAnnouncementError, match="ambiguous revision"),
    ):
        source.fetch(
            instrument_id=INSTRUMENT,
            start=date(2024, 3, 1),
            end=date(2024, 3, 31),
            retrieved_at=RETRIEVED_AT,
        )

    assert [
        int(request.url.params["page_index"])
        for request in requests
        if request.url.host == "np-anotice-stock.eastmoney.com"
    ] == [1, 2]


def test_duplicate_art_code_with_changed_payload_fails_when_count_could_complete() -> None:
    newest = _announcement("AN202403151626765999")
    middle = _announcement("AN202403141626765998")
    oldest = _announcement("AN202403131626765997")
    page_one = _body([newest, middle], page_index=1, page_size=2, total_hits=3)
    changed_duplicate = {**middle, "source_type": "duplicate-page-value"}
    page_two = _body(
        [changed_duplicate, oldest],
        page_index=2,
        page_size=2,
        total_hits=3,
    )

    with (
        EastmoneyAnnouncementSource(
            transport=_transport_for_bodies({1: page_one, 2: page_two}),
            page_size=2,
            max_pages=2,
        ) as source,
        pytest.raises(EastmoneyAnnouncementError, match="ambiguous revision"),
    ):
        source.fetch(
            instrument_id=INSTRUMENT,
            start=date(2024, 3, 1),
            end=date(2024, 3, 31),
            retrieved_at=RETRIEVED_AT,
        )


def test_fetch_batch_freezes_complete_pagination_and_per_code_query_evidence() -> None:
    annual = _announcement(
        "AN202403151626765999",
        title="东方财富:2023年年度报告",
        column_code="001001001001001",
        column_name="年度报告全文",
    )
    other_one = _announcement("AN202403141626765998")
    other_two = _announcement("AN202403131626765997")
    page_one = _body([annual, other_one], page_index=1, page_size=2, total_hits=3)
    page_two = _body([other_two], page_index=2, page_size=2, total_hits=3)

    with EastmoneyAnnouncementSource(
        transport=_transport_for_bodies({1: page_one, 2: page_two}),
        page_size=2,
        max_pages=2,
    ) as source:
        batch = source.fetch_batch(
            instrument_id=INSTRUMENT,
            start=date(2024, 3, 1),
            end=date(2024, 3, 31),
            retrieved_at=RETRIEVED_AT,
            requested_event_codes=("event.financial_results.annual_report",),
        )

    assert [item.provider_event_id for item in batch.observations] == [annual["art_code"]]
    evidence = batch.acquisition_evidence
    assert evidence["querySucceeded"] is True
    pagination = cast(dict[str, Any], evidence["pagination"])
    assert pagination["pageCount"] == 2
    assert pagination["totalHits"] == 3
    assert pagination["uniqueArtCodes"] == 3
    assert pagination["returnedRows"] == 3
    assert pagination["pages"] == [
        {
            "pageIndex": 1,
            "rowCount": 2,
            "responseSha256": hashlib.sha256(page_one).hexdigest(),
        },
        {
            "pageIndex": 2,
            "rowCount": 1,
            "responseSha256": hashlib.sha256(page_two).hexdigest(),
        },
    ]
    event_code_coverage = cast(dict[str, Any], evidence["eventCodeCoverage"])
    contract = eastmoney_event_coverage_contract("event.financial_results.annual_report")
    assert event_code_coverage["event.financial_results.annual_report"] == {
        "querySucceeded": True,
        "coverageBasis": "complete_announcement_interval_with_provider_column_classification",
        "providerColumnCodes": ["001001001001001"],
        "titleRuleIds": [],
        "classifierVersion": ANNOUNCEMENT_CLASSIFIER_VERSION,
        "classifierSha256": contract.classifier_sha256,
        "selectedRecordCount": 1,
    }
    assert len(evidence["requestSha256"]) == 64
    assert len(evidence["evidenceSha256"]) == 64


def test_announcement_list_and_content_requests_share_per_host_throttle() -> None:
    annual = _announcement(
        "AN202403151626765999",
        title="东方财富:2023年年度报告",
        column_code="001001001001001",
        column_name="年度报告全文",
    )
    semiannual = _announcement(
        "AN202403151626765998",
        title="东方财富:2023年半年度报告",
        column_code="001001001002001",
        column_name="半年度报告全文",
    )
    page_one = _body([annual], page_index=1, page_size=1, total_hits=2)
    page_two = _body([semiannual], page_index=2, page_size=1, total_hits=2)
    now = [0.0]
    sleeps: list[float] = []

    def monotonic() -> float:
        return now[0]

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    throttle = HostThrottle(
        monotonic=monotonic,
        sleeper=sleep,
        jitter=lambda: 0.0,
    )

    with EastmoneyAnnouncementSource(
        transport=_transport_for_bodies({1: page_one, 2: page_two}),
        page_size=1,
        max_pages=2,
        request_throttle=throttle,
    ) as source:
        batch = source.fetch_batch(
            instrument_id=INSTRUMENT,
            start=date(2024, 3, 15),
            end=date(2024, 3, 15),
            retrieved_at=RETRIEVED_AT,
            requested_event_codes=(
                "event.financial_results.annual_report",
                "event.financial_results.semiannual_report",
            ),
        )

    assert len(batch.observations) == 2
    assert sleeps == [1.0, 1.0]


def test_provider_column_selection_runs_after_complete_pagination_before_content_fetch() -> None:
    unrelated = _announcement(
        "AN202105201492853421",
        title="东方财富:无关的投资者关系活动记录",
        display_time="2021-05-20 20:06:34:000",
        ei_time="2021-05-20 20:06:34:000",
        notice_date="2021-05-20 00:00:00",
        column_code="050003",
        column_name="调研活动",
    )
    implementation = _announcement(
        "AN202105191492678666",
        title="东方财富:东方财富信息股份有限公司2020年度权益分派实施公告",
        display_time="2021-05-19 19:59:04:000",
        ei_time="2021-05-19 19:59:04:000",
        notice_date="2021-05-20 00:00:00",
        column_code="001002002001005",
        column_name="分配方案实施",
    )
    page_one = _body([unrelated], page_index=1, page_size=1, total_hits=2)
    page_two = _body([implementation], page_index=2, page_size=1, total_hits=2)
    requests: list[httpx.Request] = []

    with EastmoneyAnnouncementSource(
        transport=_transport_for_bodies(
            {1: page_one, 2: page_two},
            requests,
            content_bodies={
                str(implementation["art_code"]): _content_body(
                    str(implementation["art_code"]),
                    notice_title=str(implementation["title"]),
                )
            },
        ),
        page_size=1,
        max_pages=2,
        extract_document_text=True,
    ) as source:
        batch = source.fetch_batch(
            instrument_id=INSTRUMENT,
            start=date(2021, 5, 20),
            end=date(2021, 5, 20),
            retrieved_at=RETRIEVED_AT,
            requested_provider_column_codes=("001002002001005",),
        )

    assert [item.provider_event_id for item in batch.observations] == [implementation["art_code"]]
    list_requests = [
        request for request in requests if request.url.host == "np-anotice-stock.eastmoney.com"
    ]
    assert [request.url.params["page_index"] for request in list_requests] == ["1", "2"]
    content_requests = [
        request for request in requests if request.url.host == "np-cnotice-stock.eastmoney.com"
    ]
    assert [request.url.params["art_code"] for request in content_requests] == [
        implementation["art_code"]
    ]
    pagination = cast(dict[str, Any], batch.acquisition_evidence["pagination"])
    assert pagination["totalHits"] == 2
    assert pagination["uniqueArtCodes"] == 2
    assert pagination["complete"] is True
    selection = cast(
        dict[str, Any],
        batch.acquisition_evidence["providerColumnSelection"],
    )
    assert selection == {
        "requestedColumnCodes": ["001002002001005"],
        "selectedRecordCount": 1,
        "selectionAppliedAfterCompletePagination": True,
    }


def test_provider_column_selection_and_event_code_selection_cannot_be_mixed() -> None:
    with (
        EastmoneyAnnouncementSource(
            transport=_transport_for_bodies({1: _body([])}),
        ) as source,
        pytest.raises(ValueError, match="mutually exclusive"),
    ):
        source.fetch_batch(
            instrument_id=INSTRUMENT,
            start=date(2021, 5, 20),
            end=date(2021, 5, 20),
            retrieved_at=RETRIEVED_AT,
            requested_event_codes=("event.financial_results.annual_report",),
            requested_provider_column_codes=("001002002001005",),
        )


@pytest.mark.parametrize(
    "title",
    (
        "东方财富:2023年年度报告摘要",
        "东方财富:2023年年度报告（英文版）",
        "东方财富:2023年年度报告（更正版）",
        "东方财富:2023年年度报告（修订版）",
        "东方财富:关于取消2023年年度报告的公告",
    ),
)
def test_annual_full_report_column_rejects_non_initial_document_variants(
    title: str,
) -> None:
    variant = _announcement(
        "AN202403141626765931",
        title=title,
        column_code="001001001001001",
        column_name="年度报告全文",
    )
    requests: list[httpx.Request] = []
    with EastmoneyAnnouncementSource(
        transport=_transport_for_bodies({1: _body([variant])}, requests),
    ) as source:
        batch = source.fetch_batch(
            instrument_id=INSTRUMENT,
            start=date(2024, 3, 14),
            end=date(2024, 3, 14),
            retrieved_at=RETRIEVED_AT,
            requested_event_codes=("event.financial_results.annual_report",),
        )

    assert batch.observations == ()
    assert [request.url.host for request in requests] == ["np-anotice-stock.eastmoney.com"]


def test_annual_summary_cannot_trigger_before_the_initial_complete_report() -> None:
    summary = _announcement(
        "AN202403141626765930",
        title="东方财富:2023年年度报告摘要",
        column_code="001001001001001",
        column_name="年度报告全文",
    )
    complete = _announcement(
        "AN202403141626765931",
        title="东方财富:2023年年度报告",
        column_code="001001001001001",
        column_name="年度报告全文",
    )
    requests: list[httpx.Request] = []
    with EastmoneyAnnouncementSource(
        transport=_transport_for_bodies({1: _body([summary, complete])}, requests),
    ) as source:
        batch = source.fetch_batch(
            instrument_id=INSTRUMENT,
            start=date(2024, 3, 14),
            end=date(2024, 3, 14),
            retrieved_at=RETRIEVED_AT,
            requested_event_codes=("event.financial_results.annual_report",),
        )

    assert [item.provider_event_id for item in batch.observations] == [complete["art_code"]]
    assert batch.observations[0].attributes["document_version_role"] == ("initial_complete")
    assert [
        request.url.params.get("art_code")
        for request in requests
        if request.url.host == "np-cnotice-stock.eastmoney.com"
    ] == [complete["art_code"]]


def test_duplicate_initial_complete_annual_reports_for_one_period_fail_closed() -> None:
    first = _announcement(
        "AN202403141626765931",
        title="东方财富:2023年年度报告",
        column_code="001001001001001",
        column_name="年度报告全文",
    )
    duplicate = _announcement(
        "AN202403141626765932",
        title="东方财富:2023年年度报告",
        column_code="001001001001001",
        column_name="年度报告全文",
    )
    with (
        EastmoneyAnnouncementSource(
            transport=_transport_for_bodies({1: _body([first, duplicate])}),
        ) as source,
        pytest.raises(EastmoneyAnnouncementError, match="share one reporting period"),
    ):
        source.fetch_batch(
            instrument_id=INSTRUMENT,
            start=date(2024, 3, 14),
            end=date(2024, 3, 14),
            retrieved_at=RETRIEVED_AT,
            requested_event_codes=("event.financial_results.annual_report",),
        )


def test_repurchase_change_column_proves_complete_interval_coverage() -> None:
    other_one = _announcement(
        "AN202404251626765999",
        notice_date="2024-04-25 00:00:00",
        ei_time="2024-04-25 18:00:00:000",
    )
    other_two = _announcement(
        "AN202404251626765998",
        notice_date="2024-04-25 00:00:00",
        ei_time="2024-04-25 18:00:00:000",
    )
    change = _announcement(
        "AN202404251626765997",
        title="某公司:关于调整回购股份用途并注销的公告",
        column_code="001002007003009001002",
        column_name="回购方案修订",
        notice_date="2024-04-25 00:00:00",
        ei_time="2024-04-25 18:00:00:000",
    )
    page_one = _body([other_one, other_two], page_index=1, page_size=2, total_hits=3)
    page_two = _body([change], page_index=2, page_size=2, total_hits=3)
    requests: list[httpx.Request] = []

    with EastmoneyAnnouncementSource(
        transport=_transport_for_bodies({1: page_one, 2: page_two}, requests),
        page_size=2,
        max_pages=2,
    ) as source:
        batch = source.fetch_batch(
            instrument_id=INSTRUMENT,
            start=date(2024, 4, 1),
            end=date(2024, 4, 30),
            retrieved_at=RETRIEVED_AT,
            requested_event_codes=("event.repurchase_capital.repurchase_change",),
        )

    assert len(batch.observations) == 1
    observation = batch.observations[0]
    assert observation.provider_event_id == change["art_code"]
    assert observation.event_code == "event.repurchase_capital.repurchase_change"
    assert observation.attributes["classification_method"] == "column_code"
    assert (
        observation.attributes["classification_rule_id"] == "eastmoney-column:001002007003009001002"
    )
    assert json.loads(str(observation.attributes["raw_columns_json"])) == change["columns"]

    list_requests = [
        request for request in requests if request.url.host == "np-anotice-stock.eastmoney.com"
    ]
    assert [int(request.url.params["page_index"]) for request in list_requests] == [1, 2]
    content_requests = [
        request for request in requests if request.url.host == "np-cnotice-stock.eastmoney.com"
    ]
    assert [request.url.params["art_code"] for request in content_requests] == [change["art_code"]]

    pagination = cast(dict[str, Any], batch.acquisition_evidence["pagination"])
    assert pagination["complete"] is True
    assert pagination["pageCount"] == 2
    assert pagination["totalHits"] == 3
    assert pagination["uniqueArtCodes"] == 3
    assert pagination["returnedRows"] == 3
    by_code = cast(dict[str, Any], batch.acquisition_evidence["eventCodeCoverage"])
    contract = eastmoney_event_coverage_contract("event.repurchase_capital.repurchase_change")
    assert by_code["event.repurchase_capital.repurchase_change"] == {
        "querySucceeded": True,
        "coverageBasis": COMPLETE_PROVIDER_COLUMN_COVERAGE_BASIS,
        "providerColumnCodes": ["001002007003009001002"],
        "titleRuleIds": ["repurchase-change-v1"],
        "classifierVersion": ANNOUNCEMENT_CLASSIFIER_VERSION,
        "classifierSha256": contract.classifier_sha256,
        "selectedRecordCount": 1,
    }


def test_default_provider_event_columns_classify_into_their_declared_codes() -> None:
    assert DEFAULT_EVENT_CODES == _EXPECTED_DEFAULT_EVENT_CODES
    announcements: list[dict[str, object]] = []
    expected_counts: Counter[str] = Counter()
    sequence = 1
    for event_code, column_codes in _EXPECTED_DEFAULT_EVENT_COLUMNS.items():
        for column_code in column_codes:
            title = {
                "event.financial_results.annual_report": "东方财富:2023年年度报告",
                "event.financial_results.semiannual_report": ("东方财富:2023年半年度报告"),
                "event.financial_results.quarterly_report": (
                    "东方财富:2023年第一季度报告"
                    if column_code == "001001001003001"
                    else "东方财富:2023年第三季度报告"
                ),
                "event.financial_results.earnings_forecast_published": (
                    "东方财富:2023年度业绩预告"
                ),
                "event.financial_results.earnings_flash_report": ("东方财富:2023年度业绩快报"),
                "event.repurchase_capital.repurchase_change": (
                    "东方财富:关于调整回购股份方案的公告"
                ),
            }[event_code]
            announcements.append(
                _announcement(
                    f"AN20240315{sequence:012d}",
                    title=title,
                    column_code=column_code,
                    column_name=(
                        "回购方案修订"
                        if event_code == "event.repurchase_capital.repurchase_change"
                        else "定期报告栏目"
                    ),
                )
            )
            expected_counts[event_code] += 1
            sequence += 1
    page = _body(announcements)

    with EastmoneyAnnouncementSource(
        transport=_transport_for_bodies({1: page}),
    ) as source:
        batch = source.fetch_batch(
            instrument_id=INSTRUMENT,
            start=date(2024, 3, 1),
            end=date(2024, 3, 31),
            retrieved_at=RETRIEVED_AT,
            requested_event_codes=DEFAULT_EVENT_CODES,
        )

    assert Counter(item.event_code for item in batch.observations) == expected_counts


def test_refuses_partial_backfill_when_total_exceeds_max_pages() -> None:
    first = _announcement("AN202403151626765999")
    second = _announcement("AN202403141626765998")
    page_one = _body([first, second], page_size=2, total_hits=3)

    with (
        EastmoneyAnnouncementSource(
            transport=_transport_for_bodies({1: page_one}),
            page_size=2,
            max_pages=1,
        ) as source,
        pytest.raises(EastmoneyAnnouncementError, match="exceeds configured max_pages"),
    ):
        source.fetch(
            instrument_id=INSTRUMENT,
            start=date(2024, 3, 1),
            end=date(2024, 3, 31),
            retrieved_at=RETRIEVED_AT,
        )


def test_rejects_total_hits_that_changes_between_pages() -> None:
    first = _announcement("AN202403151626765999")
    second = _announcement("AN202403141626765998")
    third = _announcement("AN202403131626765997")
    page_one = _body([first, second], page_index=1, page_size=2, total_hits=4)
    page_two = _body([third], page_index=2, page_size=2, total_hits=5)

    with (
        EastmoneyAnnouncementSource(
            transport=_transport_for_bodies({1: page_one, 2: page_two}),
            page_size=2,
            max_pages=3,
        ) as source,
        pytest.raises(EastmoneyAnnouncementError, match="total_hits changed"),
    ):
        source.fetch(
            instrument_id=INSTRUMENT,
            start=date(2024, 3, 1),
            end=date(2024, 3, 31),
            retrieved_at=RETRIEVED_AT,
        )


@pytest.mark.parametrize(
    "body",
    (
        _body([], page_index=2, page_size=1, total_hits=0),
        _body([], page_index=1, page_size=2, total_hits=0),
    ),
)
def test_response_page_identity_must_match_the_exact_request(body: bytes) -> None:
    with (
        EastmoneyAnnouncementSource(
            transport=_transport_for_bodies({1: body}),
            page_size=1,
        ) as source,
        pytest.raises(EastmoneyAnnouncementError, match="page identity"),
    ):
        source.fetch(
            instrument_id=INSTRUMENT,
            start=date(2024, 3, 1),
            end=date(2024, 3, 31),
            retrieved_at=RETRIEVED_AT,
        )


def test_response_cannot_return_more_rows_than_requested_page_size() -> None:
    rows = [_announcement(f"AN20240315{index:012d}") for index in range(1, 4)]
    body = _body(rows, page_size=2, total_hits=3)

    with (
        EastmoneyAnnouncementSource(
            transport=_transport_for_bodies({1: body}),
            page_size=2,
        ) as source,
        pytest.raises(EastmoneyAnnouncementError, match="more rows"),
    ):
        source.fetch(
            instrument_id=INSTRUMENT,
            start=date(2024, 3, 1),
            end=date(2024, 3, 31),
            retrieved_at=RETRIEVED_AT,
        )


@pytest.mark.parametrize(
    "notice_date",
    ("2024-02-29 00:00:00", "2024-04-02 00:00:00"),
)
def test_every_returned_row_must_remain_inside_provider_interval_semantics(
    notice_date: str,
) -> None:
    item = _announcement("AN202403151626765999", notice_date=notice_date)

    with (
        EastmoneyAnnouncementSource(
            transport=_transport_for_bodies({1: _body([item])}),
        ) as source,
        pytest.raises(EastmoneyAnnouncementError, match="outside the requested date interval"),
    ):
        source.fetch(
            instrument_id=INSTRUMENT,
            start=date(2024, 3, 1),
            end=date(2024, 3, 31),
            retrieved_at=RETRIEVED_AT,
        )


@pytest.mark.parametrize("include_award", (False, True))
def test_title_classifier_evidence_covers_the_complete_announcement_interval(
    include_award: bool,
) -> None:
    unrelated = _announcement(
        "AN202403151626765998",
        title="某公司:投资者关系活动记录表",
    )
    rows = [unrelated]
    if include_award:
        rows.append(
            _announcement(
                "AN202403151626765999",
                title="某公司:关于重大项目正式中标的公告",
            )
        )
    page = _body(rows, page_size=2, total_hits=len(rows))
    event_code = "event.contracts_orders.major_contract_won"

    with EastmoneyAnnouncementSource(
        transport=_transport_for_bodies({1: page}),
        page_size=2,
    ) as source:
        batch = source.fetch_batch(
            instrument_id=INSTRUMENT,
            start=date(2024, 3, 1),
            end=date(2024, 3, 31),
            retrieved_at=RETRIEVED_AT,
            requested_event_codes=(event_code,),
        )

    assert len(batch.observations) == int(include_award)
    evidence = cast(dict[str, Any], batch.acquisition_evidence)
    pagination = cast(dict[str, Any], evidence["pagination"])
    assert pagination["totalHits"] == len(rows)
    assert pagination["uniqueArtCodes"] == len(rows)
    assert pagination["complete"] is True
    summary = cast(dict[str, Any], evidence["classificationSummary"])
    assert summary["totalRows"] == len(rows)
    assert summary["rowsByEventCode"] == ({event_code: 1} if include_award else {})
    by_code = cast(dict[str, dict[str, Any]], evidence["eventCodeCoverage"])
    assert by_code[event_code]["coverageBasis"] == (
        "complete_announcement_interval_with_deterministic_title_classification"
    )
    assert by_code[event_code]["selectedRecordCount"] == int(include_award)


def test_client_and_transport_cannot_both_be_injected() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(500, request=request),
    )
    with (
        httpx.Client(transport=transport) as client,
        pytest.raises(ValueError, match="not both"),
    ):
        EastmoneyAnnouncementSource(client=client, transport=transport)


def test_default_event_zero_result_keeps_pagination_and_code_level_evidence() -> None:
    called: list[str] = []
    empty_page = _body([])

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request.url.host)
        return httpx.Response(
            200,
            content=empty_page,
            headers={"Content-Type": "application/json"},
            request=request,
        )

    with EastmoneyAnnouncementSource(transport=httpx.MockTransport(handler)) as source:
        batch = source.fetch_batch(
            instrument_id=INSTRUMENT,
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            retrieved_at=RETRIEVED_AT,
            requested_event_codes=DEFAULT_EVENT_CODES,
        )
        assert batch.observations == ()
        pagination = cast(dict[str, Any], batch.acquisition_evidence["pagination"])
        assert pagination["complete"] is True
        assert pagination["zeroResult"] is True
        assert pagination["totalHits"] == 0
        assert pagination["uniqueArtCodes"] == 0
        assert pagination["pages"] == [
            {
                "pageIndex": 1,
                "rowCount": 0,
                "responseSha256": hashlib.sha256(empty_page).hexdigest(),
            }
        ]
        by_code = cast(dict[str, Any], batch.acquisition_evidence["eventCodeCoverage"])
        assert by_code == {
            event_code: {
                "querySucceeded": True,
                "coverageBasis": COMPLETE_PROVIDER_COLUMN_COVERAGE_BASIS,
                "providerColumnCodes": list(column_codes),
                "titleRuleIds": list(eastmoney_event_coverage_contract(event_code).title_rule_ids),
                "classifierVersion": ANNOUNCEMENT_CLASSIFIER_VERSION,
                "classifierSha256": eastmoney_event_coverage_contract(event_code).classifier_sha256,
                "selectedRecordCount": 0,
            }
            for event_code, column_codes in sorted(_EXPECTED_DEFAULT_EVENT_COLUMNS.items())
        }

    assert called == ["np-anotice-stock.eastmoney.com"]


@pytest.mark.parametrize(
    ("title", "column_name", "expected"),
    [
        (
            "某公司:关于2025年度现金分红预案的公告",
            "利润分配",
            "event.dividends_corporate_actions.cash_dividend_proposal",
        ),
        (
            "某公司:首次公开发行前已发行股份上市流通提示性公告",
            "限售股份上市流通",
            "event.restricted_shares_pledges.restricted_shares_unlock",
        ),
        (
            "某公司:控股股东减持股份预披露公告",
            "股份减持",
            "event.shareholder_holdings.major_holder_decrease_plan",
        ),
        (
            "某公司:控股股东减持计划实施完成的公告",
            "股份减持",
            "event.shareholder_holdings.major_holder_decrease_progress",
        ),
        (
            "某公司:关于首次回购公司股份的公告",
            "股份回购",
            "event.repurchase_capital.repurchase_first_execution",
        ),
        (
            "某公司:关于回购股份实施结果暨股份变动的公告",
            "股份回购",
            "event.repurchase_capital.repurchase_completion",
        ),
        (
            "某公司:关于终止回购公司股份的公告",
            "股份回购",
            "event.repurchase_capital.repurchase_termination",
        ),
        (
            "某公司:2026年限制性股票激励计划首次授予公告",
            "股权激励",
            "event.governance_personnel.equity_incentive_grant",
        ),
        (
            "某公司:关于收到行政处罚决定书的公告",
            "风险提示",
            "event.regulation_risk.administrative_penalty",
        ),
        (
            "某公司:关于新增重大诉讼的公告",
            "诉讼事项",
            "event.litigation_credit.major_litigation",
        ),
        (
            "某公司:关于董事长辞职暨选举新任董事长的公告",
            "高管变动",
            "event.governance_personnel.chairman_change",
        ),
        (
            "某公司:关于终止重大资产重组事项的公告",
            "资产重组",
            "event.m_and_a_restructuring.restructuring_terminated",
        ),
        (
            "某公司:发行股份购买资产事项获得证监会同意注册的公告",
            "资产重组",
            "event.m_and_a_restructuring.restructuring_regulatory_approval",
        ),
        (
            "某公司:关于签订重大经营合同的公告",
            "重大合同",
            "event.contracts_orders.major_contract_signed",
        ),
        (
            "某公司:关于重大项目中标的公告",
            "中标公告",
            "event.contracts_orders.major_contract_won",
        ),
        (
            "某公司:关于债券未能按期兑付本息的公告",
            "债务风险",
            "event.litigation_credit.debt_default",
        ),
    ],
)
def test_high_confidence_title_rules_map_explicit_event_stages(
    title: str,
    column_name: str,
    expected: str,
) -> None:
    item = _announcement(
        "AN202403141626765931",
        title=title,
        column_code="unmapped-column",
        column_name=column_name,
    )

    observation, _, _ = _fetch_one(item)

    assert observation.event_code == expected
    assert observation.attributes["classification_method"] == "deterministic_title_rule"
    assert observation.attributes["classification_rule_id"]


@pytest.mark.parametrize(
    "title",
    [
        "某公司:关于回购事项的提示性公告",
        "某公司:关于筹划重大事项的公告",
        "某公司:关于合同事项的公告",
        "某公司:2025年度利润分配预案公告",
        "某公司:关于收到行政处罚事先告知书的公告",
        "某公司:关于副总经理辞职的公告",
        "某公司:关于向特定对象增发股票的提示性公告",
        "某公司:关于回购注销部分限制性股票的公告",
        "某公司:重大项目中标候选人公示",
        "某公司:重大项目未中标的公告",
        "某公司:重大项目预中标提示性公告",
        "某公司:现金分红方案未获股东大会通过",
        "某公司:回购股份未实施完成",
        "某公司:重大资产重组未获证监会同意注册",
        "某公司:未被立案调查的说明",
        "某公司:不终止回购股份的公告",
        "某公司:总经理辞职传闻不实的澄清",
        "某公司:公司不存在债务违约",
    ],
)
def test_ambiguous_or_unsupported_titles_remain_unclassified(title: str) -> None:
    item = _announcement(
        "AN202403141626765931",
        title=title,
        column_code="unmapped-column",
        column_name="其他",
    )

    observation, _, _ = _fetch_one(item)

    assert observation.event_code == "event.announcement.unclassified"
    assert observation.attributes["classification_method"] == "unclassified"
    assert observation.attributes["classification_rule_id"] is None


def test_negative_title_gate_runs_before_provider_column_mapping() -> None:
    item = _announcement(
        "AN202403141626765931",
        title="某公司:2025年度业绩预告尚未披露的说明",
        column_code="001002004001",
        column_name="业绩预告",
    )

    observation, _, _ = _fetch_one(item)

    assert observation.event_code == "event.announcement.unclassified"
    assert observation.attributes["classification_method"] == "unclassified"


def test_contract_progress_takes_priority_over_incidental_award_wording() -> None:
    item = _announcement(
        "AN202403141626765931",
        title="某公司:重大合同履行进展暨中标项目情况公告",
        column_code="unmapped-column",
        column_name="重大合同",
    )

    observation, _, _ = _fetch_one(item)

    assert observation.event_code == "event.contracts_orders.contract_progress"
    assert observation.attributes["classification_rule_id"] == "major-contract-progress-v1"


@pytest.mark.parametrize(
    "title",
    (
        "东方财富:东方财富信息股份有限公司关于公司监事股份减持计划实施完毕的公告",
        "东方财富:东方财富信息股份有限公司关于公司高级管理人员股份减持计划实施完毕的公告",
    ),
)
def test_executive_reduction_completion_is_not_mislabeled_as_major_holder(
    title: str,
) -> None:
    observation, _, _ = _fetch_one(
        _announcement(
            "AN202403141626765931",
            title=title,
            column_code="001002008",
            column_name="其他",
        )
    )

    assert observation.event_code == "event.shareholder_holdings.executive_decrease"
    assert observation.attributes["classification_rule_id"] == "executive-decrease-v1"


def test_executive_reduction_pre_disclosure_is_not_an_executed_reduction() -> None:
    observation, _, _ = _fetch_one(
        _announcement(
            "AN202403141626765931",
            title="某公司:关于高级管理人员减持股份的预披露公告",
            column_code="001002008",
            column_name="其他",
        )
    )

    assert observation.event_code == "event.announcement.unclassified"
    assert observation.attributes["classification_method"] == "unclassified"


@pytest.mark.parametrize(
    "title",
    (
        "某公司:2024年限制性股票激励计划首次授予限制性股票第二个归属期归属结果暨股份上市的公告",
        "某公司:2024年限制性股票激励计划首次授予部分归属条件成就的公告",
    ),
)
def test_incentive_vesting_is_not_mislabeled_as_initial_grant(title: str) -> None:
    observation, _, _ = _fetch_one(
        _announcement(
            "AN202403141626765931",
            title=title,
            column_code="001002008",
            column_name="其他",
        )
    )

    assert observation.event_code == "event.announcement.unclassified"
    assert observation.attributes["classification_method"] == "unclassified"


def test_display_millisecond_drift_rounds_to_same_conservative_second() -> None:
    first = _announcement(
        "AN202403141626765931",
        display_time="2024-03-14 20:57:33:001",
        ei_time="2024-03-14 20:57:33:000",
    )
    second = {**first, "display_time": "2024-03-14 20:57:33:999"}

    first_observation, _, _ = _fetch_one(first)
    second_observation, _, _ = _fetch_one(second)

    expected = datetime(2024, 3, 14, 20, 57, 34, tzinfo=SHANGHAI)
    assert first_observation.source_released_at == expected
    assert second_observation.source_released_at == expected
    assert market_available_at(first_observation) == expected
    assert market_available_at(second_observation) == expected
    assert first_observation.attributes["conservative_available_at"] == expected.isoformat()
    assert second_observation.attributes["conservative_available_at"] == expected.isoformat()


def test_content_eitime_is_included_in_conservative_maximum() -> None:
    item = _announcement(
        "AN202403141626765931",
        display_time="2024-03-14 20:57:33:100",
        ei_time="2024-03-14 20:57:34:000",
    )
    content_body = _content_body(
        str(item["art_code"]),
        eitime="2024-03-14 20:57:35",
    )

    observation, _, _ = _fetch_one(item, content_body=content_body)

    expected = datetime(2024, 3, 14, 20, 57, 35, tzinfo=SHANGHAI)
    assert observation.vendor_first_available_at == expected
    assert market_available_at(observation) == expected
    assert observation.attributes["raw_content_ei_time"] == "2024-03-14 20:57:35"


def test_malformed_content_eitime_rejects_observation_instead_of_guessing() -> None:
    item = _announcement("AN202403141626765931")
    content_body = _content_body(
        str(item["art_code"]),
        eitime="not-a-provider-time",
    )

    observation, _, _ = _fetch_one(item, content_body=content_body)

    assert observation.validation_status == "rejected"
    assert observation.time_quality is TimeQuality.DATE_ONLY_CONSERVATIVE
    assert market_available_at(observation) is None


def test_notice_content_is_frozen_verbatim_and_hashed_as_document() -> None:
    item = _announcement("AN202403141626765931")
    notice_content = "\n  第一行\n第二行  \n"
    content_body = _content_body(
        str(item["art_code"]),
        notice_content=notice_content,
        eitime="2024-03-14 20:57:33",
    )

    observation, _, _ = _fetch_one(item, content_body=content_body)

    expected = hashlib.sha256(notice_content.encode()).hexdigest()
    assert observation.document_sha256 == expected
    assert observation.attributes["raw_notice_content_sha256"] == expected
    assert (
        observation.attributes["raw_content_response_sha256"]
        == hashlib.sha256(content_body).hexdigest()
    )
    raw_content = json.loads(str(observation.attributes["raw_content_payload_json"]))
    assert raw_content["notice_content"] == notice_content


def test_notice_text_extraction_is_opt_in_and_preserves_raw_document_identity() -> None:
    item = _announcement("AN202403141626765931")
    notice_content = "\r\n  年报提到ＡＩ ６次。\r\n"
    content_body = _content_body(
        str(item["art_code"]),
        notice_content=notice_content,
        eitime="2024-03-14 20:57:33",
    )

    observation, _, _ = _fetch_one(
        item,
        content_body=content_body,
        extract_document_text=True,
    )

    raw_sha256 = hashlib.sha256(notice_content.encode()).hexdigest()
    assert observation.document_sha256 == raw_sha256
    assert observation.attributes["document_text_source_sha256"] == raw_sha256
    assert observation.attributes["document_text"] == "年报提到AI 6次。"
    assert observation.attributes["document_text_page_count"] == 1
    assert observation.attributes["document_text_provider_page_count"] == 1
    assert observation.attributes["document_text_extracted_page_count"] == 1
    assert observation.attributes["document_text_empty_page_count"] == 0
    assert observation.attributes["document_text_quality"] == "complete_text_layer"
    assert observation.attributes["document_text_normalization"] == ("unicode_nfkc_lf_strip.v1")
    pages = json.loads(str(observation.attributes["document_text_pages_json"]))
    assert [page["text"] for page in pages] == ["年报提到AI 6次。"]


def test_initial_complete_annual_report_can_publish_matching_document_text() -> None:
    title = "东方财富:2023年年度报告"
    item = _announcement(
        "AN202403141626765931",
        title=title,
        column_code="001001001001001",
        column_name="年度报告全文",
    )
    content_body = _content_body(
        str(item["art_code"]),
        notice_content="完整年报正文 AI AI AI AI AI AI",
        notice_title=title,
    )

    observation, _, _ = _fetch_one(
        item,
        content_body=content_body,
        extract_document_text=True,
    )

    assert observation.event_code == "event.financial_results.annual_report"
    assert observation.attributes["document_version_role"] == "initial_complete"
    assert observation.attributes["document_text_scope"] == "complete_primary_document"
    assert observation.attributes["document_text"] == "完整年报正文 AI AI AI AI AI AI"


@pytest.mark.parametrize(
    "content_title",
    [
        "东方财富:2023年年度报告（修订版）",
        "东方财富:2023年年度报告摘要",
        "",
    ],
)
def test_document_text_requires_the_same_initial_complete_title_from_content_api(
    content_title: str,
) -> None:
    item = _announcement(
        "AN202403141626765931",
        title="东方财富:2023年年度报告",
        column_code="001001001001001",
        column_name="年度报告全文",
    )
    content_body = _content_body(
        str(item["art_code"]),
        notice_content="完整年报正文 AI AI AI AI AI AI",
        notice_title=content_title,
    )

    with pytest.raises(
        EastmoneyAnnouncementError,
        match=(
            "content.data.notice_title must be a non-empty string"
            if not content_title
            else "content title does not match list response"
        ),
    ):
        _fetch_one(
            item,
            content_body=content_body,
            extract_document_text=True,
        )


def test_default_ingestion_does_not_publish_document_text_attributes() -> None:
    observation, _, _ = _fetch_one(_announcement("AN202403141626765931"))

    assert "document_text" not in observation.attributes
    assert "document_text_sha256" not in observation.attributes


def test_blank_notice_content_falls_back_to_frozen_pdf_bytes() -> None:
    item = _announcement("AN202403141626765931")
    art_code = str(item["art_code"])
    pdf_body = b"%PDF-1.7\nmock announcement\n%%EOF"
    content_body = _content_body(art_code, notice_content="")

    observation, _, requests = _fetch_one(
        item,
        content_body=content_body,
        pdf_body=pdf_body,
    )

    expected = hashlib.sha256(pdf_body).hexdigest()
    assert observation.document_url == f"https://pdf.dfcfw.com/pdf/H2_{art_code}_1.pdf"
    assert observation.document_sha256 == expected
    assert observation.attributes["document_hash_basis"] == "pdf_bytes"
    assert observation.attributes["raw_pdf_response_sha256"] == expected
    assert [request.url.host for request in requests] == [
        "np-anotice-stock.eastmoney.com",
        "np-cnotice-stock.eastmoney.com",
        "pdf.dfcfw.com",
    ]


def test_multipage_notice_content_uses_complete_pdf_not_first_page_text() -> None:
    item = _announcement("AN202403141626765931")
    art_code = str(item["art_code"])
    pdf_body = b"%PDF-1.7\ncomplete annual report\n%%EOF"
    decoded = json.loads(_content_body(art_code, notice_content="only page one"))
    decoded["data"]["page_size"] = 79
    content_body = json.dumps(decoded, ensure_ascii=False).encode()

    observation, _, _ = _fetch_one(
        item,
        content_body=content_body,
        pdf_body=pdf_body,
    )

    assert observation.document_sha256 == hashlib.sha256(pdf_body).hexdigest()
    assert observation.attributes["document_hash_basis"] == "pdf_bytes"
    assert (
        observation.attributes["raw_notice_content_sha256"]
        == hashlib.sha256(b"only page one").hexdigest()
    )


class _RecordingPdfTextExtractor(AnnouncementDocumentTextExtractor):
    def __init__(self) -> None:
        self.expected_page_count: int | None = None

    def extract_pdf(
        self,
        pdf_bytes: bytes,
        *,
        expected_page_count: int | None = None,
    ) -> ExtractedDocumentText:
        self.expected_page_count = expected_page_count
        page_texts = ("第一页AI", "第二页正文")
        pages = tuple(
            ExtractedTextPage(
                page_number=index,
                text=text,
                text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                character_count=len(text),
                non_whitespace_character_count=len(text),
            )
            for index, text in enumerate(page_texts, start=1)
        )
        full_text = "\n\f\n".join(page_texts)
        return ExtractedDocumentText(
            source_format="pdf",
            source_document_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            pages=pages,
            text=full_text,
            text_sha256=hashlib.sha256(full_text.encode()).hexdigest(),
            extractor_name="test_pdf_text_layer",
            extractor_version="1",
            extractor_library_version="test",
        )


def test_multipage_pdf_text_extraction_preserves_pdf_hash_and_page_metadata() -> None:
    item = _announcement("AN202403141626765931")
    art_code = str(item["art_code"])
    pdf_body = b"%PDF-1.7\ncomplete annual report\n%%EOF"
    decoded = json.loads(_content_body(art_code, notice_content="only page one"))
    decoded["data"]["page_size"] = 2
    content_body = json.dumps(decoded, ensure_ascii=False).encode()
    extractor = _RecordingPdfTextExtractor()

    observation, _, _ = _fetch_one(
        item,
        content_body=content_body,
        pdf_body=pdf_body,
        extract_document_text=True,
        document_text_extractor=extractor,
    )

    pdf_sha256 = hashlib.sha256(pdf_body).hexdigest()
    assert extractor.expected_page_count == 2
    assert observation.document_sha256 == pdf_sha256
    assert observation.attributes["raw_pdf_response_sha256"] == pdf_sha256
    assert observation.attributes["document_text_source_sha256"] == pdf_sha256
    assert observation.attributes["document_text"] == "第一页AI\n\f\n第二页正文"
    assert observation.attributes["document_text_page_count"] == 2
    assert observation.attributes["document_text_provider_page_count"] == 2
    assert observation.attributes["document_text_extractor_library_version"] == "test"
    pages = json.loads(str(observation.attributes["document_text_pages_json"]))
    assert [page["text"] for page in pages] == ["第一页AI", "第二页正文"]


def test_content_http_failure_fails_closed_without_list_only_observation() -> None:
    item = _announcement("AN202403141626765931")

    with pytest.raises(EastmoneyAnnouncementError, match=r"content .* failed"):
        _fetch_one(item, content_status=503)


def test_content_title_must_match_the_frozen_list_record() -> None:
    item = _announcement(
        "AN202403141626765931",
        title="某公司:关于重大项目正式中标的公告",
    )
    mismatched = _content_body(
        str(item["art_code"]),
        notice_title="某公司:关于重大项目中标候选人公示",
    )

    with pytest.raises(EastmoneyAnnouncementError, match="content title"):
        _fetch_one(item, content_body=mismatched)


def test_missing_notice_content_and_pdf_fails_closed() -> None:
    item = _announcement("AN202403141626765931")
    art_code = str(item["art_code"])
    content = json.loads(_content_body(art_code, notice_content=""))
    content["data"]["attach_list"] = []
    content["data"]["attach_url"] = ""
    content["data"]["attach_url_web"] = ""
    content_body = json.dumps(content, ensure_ascii=False).encode()

    with pytest.raises(EastmoneyAnnouncementError, match="complete document"):
        _fetch_one(item, content_body=content_body)


def test_content_art_code_mismatch_fails_closed() -> None:
    item = _announcement("AN202403141626765931")
    mismatched = _content_body("AN202403141626765932")

    with pytest.raises(EastmoneyAnnouncementError, match="art_code does not match"):
        _fetch_one(item, content_body=mismatched)


def test_mock_payload_remains_json_serializable() -> None:
    """Guard the redacted provider fixture against accidental non-JSON data."""

    item: dict[str, Any] = _announcement("AN202403141626765931")
    assert json.loads(json.dumps(item, ensure_ascii=False))["art_code"] == item["art_code"]
