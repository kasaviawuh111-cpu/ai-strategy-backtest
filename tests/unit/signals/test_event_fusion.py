from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.domain.events import (
    ANNOUNCEMENT_EVENT_PROVIDERS,
    EVENT_LANE_SOURCE_POLICIES,
    LICENSE_APPROVAL_EVENT_CODE,
    MAJOR_CONTRACT_EVENT_CODE,
    CoverageStatus,
    EventObservation,
    fuse_event_observations,
    market_available_at,
)
from ashare_lab.domain.market_data import TimeQuality
from ashare_lab.domain.shared import DomainValidationError, InstrumentId

SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")
INSTRUMENT = InstrumentId("300059.SZ")
EVENT_CODE = "event.financial_results.annual_report"


def observation(
    provider: str,
    provider_event_id: str,
    *,
    released_at: datetime | None = None,
    vendor_at: datetime | None = None,
    retrieved_at: datetime | None = None,
    title: str = "东方财富2024年年度报告",
    document_hash: str | None = "a" * 64,
    event_code: str = EVENT_CODE,
    instrument_id: InstrumentId = INSTRUMENT,
    occurred_at: datetime | None = None,
    external_fact_id: str | int | Decimal | bool | None = None,
    quality: TimeQuality = TimeQuality.EXACT,
    validation_status: str = "validated",
    attributes: dict[str, str | int | Decimal | bool | None] | None = None,
) -> EventObservation:
    released = released_at or datetime(2025, 3, 15, 20, 0, tzinfo=SHANGHAI)
    retrieved = retrieved_at or datetime(2025, 3, 16, 8, 0, tzinfo=SHANGHAI)
    return EventObservation(
        provider=provider,
        provider_event_id=provider_event_id,
        instrument_id=instrument_id,
        event_code=event_code,
        title=title,
        occurred_at=occurred_at or datetime(2024, 12, 31, 0, 0, tzinfo=SHANGHAI),
        source_released_at=released,
        vendor_first_available_at=vendor_at,
        retrieved_at=retrieved,
        time_quality=quality,
        document_url=f"https://example.test/{provider_event_id}",
        document_sha256=document_hash,
        raw_response_sha256="b" * 64,
        external_fact_id=external_fact_id,
        validation_status=validation_status,
        attributes=({"report_period": "2024-12-31"} if attributes is None else attributes),
    )


def test_observation_rejects_naive_datetimes_and_normalizes_hashes() -> None:
    with pytest.raises(DomainValidationError, match="timezone-aware"):
        observation(
            "eastmoney",
            "em-1",
            retrieved_at=datetime(2025, 3, 16, 8, 0),
        )

    item = replace(
        observation("eastmoney", "em-1"),
        document_sha256="sha256:" + "A" * 64,
    )
    assert item.document_sha256 == "a" * 64
    with pytest.raises(TypeError):
        item.attributes["new"] = "forbidden"  # type: ignore[index]


def test_web_archive_is_an_acquisition_provider_but_search_hits_are_rejected() -> None:
    archived = observation("web_archive", "archive-1")

    assert archived.provider == "web_archive"
    assert tuple(ANNOUNCEMENT_EVENT_PROVIDERS) == (
        "eastmoney",
        "ifind",
        "rqdata",
        "tushare",
    )
    with pytest.raises(DomainValidationError, match="search hits"):
        observation(
            "web_archive",
            "search-hit-1",
            attributes={"search_hit_url": "https://search.example.test/result"},
        )


def test_event_lane_policies_are_explicit_and_never_timestamp_selected() -> None:
    contract_policy = EVENT_LANE_SOURCE_POLICIES[MAJOR_CONTRACT_EVENT_CODE]
    license_policy = EVENT_LANE_SOURCE_POLICIES[LICENSE_APPROVAL_EVENT_CODE]
    assert contract_policy.evidence_preference == (
        "issuer_announcement_history",
        "news_history",
    )
    assert contract_policy.provider_priority[:4] == ANNOUNCEMENT_EVENT_PROVIDERS
    assert license_policy.evidence_preference == ("regulator_history", "news_history")
    assert license_policy.provider_priority[0] == "web_archive"

    earlier_archive_contract = observation(
        "web_archive",
        "archive-contract",
        event_code=MAJOR_CONTRACT_EVENT_CODE,
        released_at=datetime(2025, 3, 15, 8, 0, tzinfo=SHANGHAI),
    )
    later_eastmoney_contract = observation(
        "eastmoney",
        "em-contract",
        event_code=MAJOR_CONTRACT_EVENT_CODE,
        released_at=datetime(2025, 3, 15, 20, 0, tzinfo=SHANGHAI),
    )
    contract = fuse_event_observations((earlier_archive_contract, later_eastmoney_contract)).events[
        0
    ]
    assert contract.selected_provider == "eastmoney"
    assert contract.market_available_at == datetime(2025, 3, 15, 20, 0, tzinfo=SHANGHAI)

    earlier_eastmoney_license = observation(
        "eastmoney",
        "em-license",
        event_code=LICENSE_APPROVAL_EVENT_CODE,
        released_at=datetime(2025, 3, 15, 8, 0, tzinfo=SHANGHAI),
    )
    later_regulator_archive = observation(
        "web_archive",
        "archive-license",
        event_code=LICENSE_APPROVAL_EVENT_CODE,
        released_at=datetime(2025, 3, 15, 21, 0, tzinfo=SHANGHAI),
    )
    license_event = fuse_event_observations(
        (earlier_eastmoney_license, later_regulator_archive)
    ).events[0]
    assert license_event.selected_provider == "web_archive"
    assert license_event.market_available_at == datetime(
        2025,
        3,
        15,
        21,
        0,
        tzinfo=SHANGHAI,
    )


def test_fixed_primary_source_wins_instead_of_earliest_timestamp() -> None:
    eastmoney = observation(
        "eastmoney",
        "em-1",
        released_at=datetime(2025, 3, 15, 20, 0, tzinfo=SHANGHAI),
        vendor_at=datetime(2025, 3, 15, 20, 1, tzinfo=SHANGHAI),
    )
    earlier_ifind = observation(
        "ifind",
        "ifind-1",
        released_at=datetime(2025, 3, 15, 18, 0, tzinfo=SHANGHAI),
        vendor_at=datetime(2025, 3, 15, 18, 1, tzinfo=SHANGHAI),
    )

    result = fuse_event_observations((earlier_ifind, eastmoney))

    assert result.coverage_status is CoverageStatus.COMPLETE
    assert len(result.events) == 1
    event = result.events[0]
    assert event.selected_provider == "eastmoney"
    assert event.market_available_at == datetime(2025, 3, 15, 20, 1, tzinfo=SHANGHAI)
    assert [item.provider for item in event.provenance] == ["eastmoney", "ifind"]


def test_fallback_priority_is_ifind_then_rqdata_then_tushare() -> None:
    ifind = observation(
        "ifind",
        "ifind-1",
        released_at=datetime(2025, 3, 15, 21, 0, tzinfo=SHANGHAI),
    )
    rqdata = observation(
        "rqdata",
        "rq-1",
        released_at=datetime(2025, 3, 15, 19, 0, tzinfo=SHANGHAI),
    )
    tushare = observation(
        "tushare",
        "ts-1",
        released_at=datetime(2025, 3, 15, 18, 0, tzinfo=SHANGHAI),
    )

    event = fuse_event_observations((tushare, rqdata, ifind)).events[0]

    assert event.selected_provider == "ifind"
    assert event.market_available_at == datetime(2025, 3, 15, 21, 0, tzinfo=SHANGHAI)
    assert event.coverage_status is CoverageStatus.PARTIAL


def test_document_hash_fuses_sources_and_complements_missing_attributes() -> None:
    eastmoney = observation(
        "eastmoney",
        "em-1",
        attributes={"report_period": "2024-12-31"},
    )
    ifind = observation(
        "ifind",
        "ifind-1",
        attributes={"report_period": "2024-12-31", "report_type": "annual"},
    )

    result = fuse_event_observations((ifind, eastmoney))

    assert len(result.events) == 1
    assert result.events[0].attributes == {
        "report_period": "2024-12-31",
        "report_type": "annual",
    }
    assert {item.provider_event_id for item in result.events[0].provenance} == {
        "em-1",
        "ifind-1",
    }


def test_external_fact_id_fuses_different_publisher_documents_first() -> None:
    issuer_notice = observation(
        "eastmoney",
        "em-contract",
        event_code=MAJOR_CONTRACT_EVENT_CODE,
        external_fact_id=" Contract-2025-001 ",
        title="关于签订重大合同的公告",
        document_hash="a" * 64,
        released_at=datetime(2025, 3, 15, 20, 0, tzinfo=SHANGHAI),
        attributes={
            "contract_amount": Decimal("500000000"),
            "publisher_name": "东方财富公告库",
            "extractor.version": "issuer-v1",
            "discovery_path": "issuer-history",
        },
    )
    archived_regulator_page = observation(
        "web_archive",
        "archive-contract",
        event_code=MAJOR_CONTRACT_EVENT_CODE,
        external_fact_id="contract-2025-001",
        title="项目中标及合同进展",
        document_hash="c" * 64,
        released_at=datetime(2025, 3, 17, 9, 0, tzinfo=SHANGHAI),
        retrieved_at=datetime(2025, 3, 18, 8, 0, tzinfo=SHANGHAI),
        attributes={
            "contract_amount": Decimal("500000000.0"),
            "publisherName": "监管网站",
            "extractor_version": "archive-v3",
            "discovery.query": "合同 中标",
        },
    )

    result = fuse_event_observations((archived_regulator_page, issuer_notice))

    assert len(result.events) == 1
    event = result.events[0]
    assert event.external_fact_id == "Contract-2025-001"
    assert event.selected_provider == "eastmoney"
    assert len(event.provenance) == 2
    assert event.conflicts == ()


def test_external_fact_id_quarantines_conflicting_fact_time() -> None:
    base = observation(
        "eastmoney",
        "em-fact",
        external_fact_id="fact-42",
    )
    contradictory = observation(
        "ifind",
        "ifind-fact",
        external_fact_id="FACT-42",
        occurred_at=datetime(2025, 1, 1, 0, 0, tzinfo=SHANGHAI),
        document_hash="c" * 64,
    )

    result = fuse_event_observations((base, contradictory))

    assert result.events == ()
    assert len(result.quarantined) == 1
    assert {item.code for item in result.conflicts} == {"occurred_at_conflict"}


def test_external_fact_identity_is_scoped_by_instrument_and_event_code() -> None:
    base = observation(
        "eastmoney",
        "em-fact",
        external_fact_id="fact-42",
        document_hash="a" * 64,
    )
    other_instrument = observation(
        "ifind",
        "ifind-other-instrument",
        external_fact_id="FACT-42",
        instrument_id=InstrumentId("600519.SH"),
        document_hash="c" * 64,
    )
    other_event = observation(
        "rqdata",
        "rq-other-event",
        external_fact_id="fact-42",
        event_code="event.financial_results.earnings_flash_report",
        document_hash="d" * 64,
    )

    result = fuse_event_observations((base, other_instrument, other_event))

    assert len(result.events) == 3
    assert result.quarantined == ()


def test_document_identity_is_scoped_by_instrument_and_event_code() -> None:
    base = observation("eastmoney", "em-document")
    other_instrument = observation(
        "ifind",
        "ifind-other-instrument",
        instrument_id=InstrumentId("600519.SH"),
    )
    other_event = observation(
        "rqdata",
        "rq-other-event",
        event_code="event.financial_results.earnings_flash_report",
    )

    result = fuse_event_observations((base, other_instrument, other_event))

    assert len(result.events) == 3
    assert result.quarantined == ()


def test_title_date_identity_is_scoped_by_event_code() -> None:
    annual_report = observation(
        "eastmoney",
        "em-title-date",
        document_hash=None,
    )
    flash_report = observation(
        "ifind",
        "ifind-title-date",
        event_code="event.financial_results.earnings_flash_report",
        document_hash=None,
    )

    result = fuse_event_observations((annual_report, flash_report))

    assert len(result.events) == 2
    assert result.quarantined == ()


def test_one_document_cannot_claim_multiple_external_facts() -> None:
    first = observation(
        "eastmoney",
        "em-fact-1",
        external_fact_id="fact-1",
    )
    second = observation(
        "ifind",
        "ifind-fact-2",
        external_fact_id="fact-2",
    )

    result = fuse_event_observations((first, second))

    assert result.events == ()
    assert len(result.quarantined) == 2
    assert {item.code for item in result.conflicts} == {"external_fact_id_conflict"}


def test_normalized_title_and_local_announcement_date_are_secondary_identity() -> None:
    eastmoney = observation(
        "eastmoney",
        "em-1",
        title=" 东方财富：2024 年年度报告 ",
        document_hash=None,
    )
    ifind = observation(
        "ifind",
        "ifind-1",
        title="东方财富:2024年年度报告",
        document_hash=None,
    )

    result = fuse_event_observations((ifind, eastmoney))

    assert len(result.events) == 1
    assert len(result.events[0].provenance) == 2


def test_provider_label_date_matches_even_when_precise_times_cross_midnight_label() -> None:
    eastmoney = observation(
        "eastmoney",
        "em-1",
        released_at=datetime(2025, 3, 18, 20, 0, tzinfo=SHANGHAI),
        retrieved_at=datetime(2025, 3, 20, 8, 0, tzinfo=SHANGHAI),
        document_hash=None,
        attributes={"raw_notice_date": "2025-03-19"},
    )
    tushare = observation(
        "tushare",
        "ts-1",
        released_at=datetime(2025, 3, 19, 8, 0, tzinfo=SHANGHAI),
        retrieved_at=datetime(2025, 3, 20, 8, 0, tzinfo=SHANGHAI),
        document_hash=None,
        attributes={"ann_date": "20250319"},
    )

    result = fuse_event_observations((tushare, eastmoney))

    assert len(result.events) == 1
    assert [item.provider for item in result.events[0].provenance] == [
        "eastmoney",
        "tushare",
    ]


def test_event_code_is_identity_scope_and_attribute_conflict_is_quarantined() -> None:
    base = observation("eastmoney", "em-1")
    wrong_code = observation(
        "ifind",
        "ifind-1",
        event_code="event.financial_results.earnings_flash_report",
    )

    code_result = fuse_event_observations((base, wrong_code))

    assert len(code_result.events) == 2
    assert code_result.quarantined == ()

    wrong_attribute = observation(
        "ifind",
        "ifind-2",
        attributes={"report_period": "2023-12-31"},
    )
    attribute_result = fuse_event_observations((base, wrong_attribute))
    assert attribute_result.events == ()
    assert {item.code for item in attribute_result.conflicts} == {"attribute_conflict"}


def test_ambiguous_title_date_match_is_isolated() -> None:
    first_document = observation("eastmoney", "em-1", document_hash="a" * 64)
    second_document = observation("ifind", "ifind-1", document_hash="c" * 64)
    without_hash = observation("tushare", "ts-1", document_hash=None)

    result = fuse_event_observations((without_hash, second_document, first_document))

    assert result.events == ()
    assert len(result.quarantined) == 3
    assert any(item.code == "ambiguous_document_match" for item in result.conflicts)
    assert result.coverage_status is CoverageStatus.CONFLICTED


def test_byte_different_cross_source_mirrors_never_emit_duplicate_events() -> None:
    first_document = observation("eastmoney", "em-1", document_hash="a" * 64)
    second_document = observation("ifind", "ifind-1", document_hash="c" * 64)

    result = fuse_event_observations((second_document, first_document))

    assert result.events == ()
    assert len(result.quarantined) == 2
    assert len({item.canonical_event_id for item in result.quarantined}) == 2
    assert {conflict.code for conflict in result.conflicts} == {"ambiguous_document_match"}
    assert result.coverage_status is CoverageStatus.CONFLICTED


def test_date_only_or_unvalidated_observation_is_retained_but_not_tradable() -> None:
    date_only = observation(
        "eastmoney",
        "em-date",
        quality=TimeQuality.DATE_ONLY_CONSERVATIVE,
        validation_status="blocked_time_quality",
    )

    result = fuse_event_observations((date_only,))

    assert result.events == ()
    assert len(result.quarantined) == 1
    assert result.quarantined[0].market_available_at is None
    assert result.quarantined[0].provenance[0].validation_status == "blocked_time_quality"
    assert result.coverage_status is CoverageStatus.UNAVAILABLE


def test_market_availability_excludes_retrieval_and_is_canonical_shanghai_time() -> None:
    released_utc = datetime(2025, 3, 15, 12, 0, tzinfo=UTC)
    much_later_retrieval = released_utc + timedelta(days=30)
    item = observation(
        "eastmoney",
        "em-1",
        released_at=released_utc,
        retrieved_at=much_later_retrieval,
    )

    fused = fuse_event_observations((item,)).events[0]

    assert market_available_at(item) == datetime(2025, 3, 15, 20, 0, tzinfo=SHANGHAI)
    assert fused.market_available_at == datetime(2025, 3, 15, 20, 0, tzinfo=SHANGHAI)
    assert fused.market_available_at != much_later_retrieval
    assert fused.provenance[0].retrieved_at == much_later_retrieval


def test_subsecond_market_time_is_conservatively_ceiled_to_next_second() -> None:
    subsecond = observation(
        "eastmoney",
        "em-1",
        released_at=datetime(2025, 3, 15, 20, 0, 0, 1, tzinfo=SHANGHAI),
    )

    result = fuse_event_observations((subsecond,))

    assert len(result.events) == 1
    assert result.events[0].market_available_at == datetime(
        2025,
        3,
        15,
        20,
        0,
        1,
        tzinfo=SHANGHAI,
    )


def test_fusion_is_input_order_invariant() -> None:
    observations = (
        observation("eastmoney", "em-1"),
        observation("ifind", "ifind-1"),
        observation("rqdata", "rq-1"),
    )

    forward = fuse_event_observations(observations)
    reverse = fuse_event_observations(tuple(reversed(observations)))

    assert forward == reverse
    assert forward.events[0].canonical_event_id == reverse.events[0].canonical_event_id
