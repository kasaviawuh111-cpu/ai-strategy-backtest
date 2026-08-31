from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.adapters.event_sources.web_evidence import (
    LICENSE_APPROVAL,
    MAJOR_CONTRACT_WON,
    TIMING_LICENSED_VENDOR,
    TIMING_PAGE_METADATA,
    TIMING_PROSPECTIVE_ARCHIVE,
    TIMING_SEARCH_INDEX,
    DiscoveryOnlyEvidenceError,
    SearchHit,
    WebEvidenceError,
    normalize_web_evidence_row,
)
from ashare_lab.domain.events import market_available_at
from ashare_lab.domain.market_data import TimeQuality
from ashare_lab.domain.shared import InstrumentId

SHANGHAI = ZoneInfo("Asia/Shanghai")
RAW_SHA256 = "a1b5d42a7fea1f166fb1488ae9ce5f4993ff7eefab2e408e56378d4eaaa4012c"
CONTENT_SHA256 = "b" * 64
RETRIEVED_AT = datetime(2026, 8, 29, 12, 0, tzinfo=SHANGHAI)


def _contract_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "provider": "ifind",
        "event_code": MAJOR_CONTRACT_WON,
        "external_fact_id": "CG-2026-001",
        "title": "某公司收到重大项目中标通知书",
        "canonical_url": "https://example.test/contracts/CG-2026-001",
        "raw_response_sha256": RAW_SHA256,
        "content_sha256": CONTENT_SHA256,
        "instrument_id": "300059.SZ",
        "entity_mapping_status": "exact",
        "entity_mapping_key": "300059.SZ",
        "entity_match_method": "exact_legal_name",
        "mapping_confidence": 1,
        "source_entity_id": "东方财富信息股份有限公司",
        "publisher_name": "某公共资源交易中心",
        "extractor_id": "contract-fields",
        "extractor_version": "contract-extractor/1.0.0",
        "classifier_id": "web-event-classifier",
        "classifier_version": "event-classifier/1.0.0",
        "revision_type": "initial",
        "revision_no": 0,
        "revision_id": "CG-2026-001:r0",
        "publisher_type": "public_procurement_platform",
        "review_status": "accepted",
        "evidence_span": "确定东方财富信息股份有限公司为该项目中标人。",
        "timing_basis": TIMING_LICENSED_VENDOR,
        "provider_license_status": "licensed",
        "source_released_at": "2026-08-28 17:43:20+08:00",
        "vendor_first_available_at": "2026-08-28 17:43:21+08:00",
        "retrieved_at": RETRIEVED_AT,
        "event_attributes": {
            "award_status": "formal_winner",
            "project_name": "数据平台建设项目",
            "awarding_entity": "某公共资源交易中心",
            "materiality_status": "material",
            "materiality_basis": "issuer_major_contract_disclosure",
        },
    }
    row.update(overrides)
    return row


def _license_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "provider": "web_archive",
        "event_code": LICENSE_APPROVAL,
        "external_fact_id": "证监许可〔2025〕689号",
        "title": "关于核准东方财富证券股份有限公司上市证券做市交易业务资格的批复",
        "canonical_url": "https://www.csrc.gov.cn/csrc/c101857/c7552337/content.shtml",
        "raw_response_sha256": RAW_SHA256,
        "content_sha256": CONTENT_SHA256,
        "instrument_id": "300059.SZ",
        "entity_mapping_status": "exact",
        "entity_mapping_key": "300059.SZ",
        "entity_match_method": "exact_legal_name",
        "mapping_confidence": "1.0",
        "source_entity_id": "东方财富证券股份有限公司",
        "publisher_name": "中国证券监督管理委员会",
        "extractor_id": "regulatory-license-fields",
        "extractor_version": "license-extractor/1.0.0",
        "classifier_id": "web-event-classifier",
        "classifier_version": "event-classifier/1.0.0",
        "revision_type": "initial",
        "revision_no": 0,
        "revision_id": "证监许可〔2025〕689号:r0",
        "publisher_type": "regulator",
        "review_status": "accepted",
        "evidence_sha256": "d" * 64,
        "timing_basis": TIMING_PROSPECTIVE_ARCHIVE,
        "capture_mode": "prospective",
        "source_released_at": "2025-04-02",
        "collector_observed_at": "2026-08-29 10:15:32+08:00",
        "retrieved_at": RETRIEVED_AT,
        "event_attributes": {
            "license_status": "approved",
            "license_name": "上市证券做市交易业务资格",
            "approving_authority": "中国证券监督管理委员会",
        },
    }
    row.update(overrides)
    return row


def test_exact_licensed_vendor_contract_is_tradable_and_auditable() -> None:
    observation = normalize_web_evidence_row(_contract_row())

    assert observation.provider == "ifind"
    assert observation.provider_event_id == "CG-2026-001"
    assert observation.external_fact_id == "CG-2026-001"
    assert observation.instrument_id == InstrumentId("300059.SZ")
    assert observation.vendor_first_available_at == datetime(
        2026, 8, 28, 17, 43, 21, tzinfo=SHANGHAI
    )
    assert observation.time_quality is TimeQuality.VENDOR_OBSERVED
    assert observation.validation_status == "validated"
    assert observation.document_url == "https://example.test/contracts/CG-2026-001"
    assert observation.document_sha256 == CONTENT_SHA256
    assert observation.raw_response_sha256 == RAW_SHA256
    assert observation.attributes["award_stage"] == "formal_winner"
    assert "award_status" not in observation.attributes
    assert observation.attributes["counterparty"] == "某公共资源交易中心"
    assert observation.attributes["extractor_id"] == "contract-fields"
    assert observation.attributes["publisher_authority"] == "authoritative"
    assert observation.attributes["review_status"] == "accepted"
    assert observation.attributes["timestamp_precision"] == "second"
    assert market_available_at(observation) == observation.vendor_first_available_at


@pytest.mark.parametrize("provider", ["eastmoney", "ifind", "rqdata", "tushare"])
def test_all_existing_providers_share_the_licensed_evidence_contract(provider: str) -> None:
    observation = normalize_web_evidence_row(_contract_row(provider=provider))

    assert observation.provider == provider
    assert observation.validation_status == "validated"


def test_prospective_archive_capture_uses_capture_time_without_backfill() -> None:
    observation = normalize_web_evidence_row(_license_row())

    assert observation.provider == "web_archive"
    assert observation.source_released_at == datetime(2025, 4, 2, 15, 0, tzinfo=SHANGHAI)
    assert observation.vendor_first_available_at == datetime(
        2026, 8, 29, 10, 15, 32, tzinfo=SHANGHAI
    )
    assert observation.time_quality is TimeQuality.VENDOR_OBSERVED
    assert observation.validation_status == "validated"
    assert observation.attributes["approval_status"] == "approved"
    assert "license_status" not in observation.attributes
    assert observation.attributes["license_type"] == "上市证券做市交易业务资格"
    assert observation.attributes["regulator"] == "中国证券监督管理委员会"
    assert observation.attributes["source_timestamp_precision"] == "date"
    assert observation.attributes["timestamp_precision"] == "second"
    assert market_available_at(observation) == datetime(2026, 8, 29, 10, 15, 32, tzinfo=SHANGHAI)


def test_real_csrc_date_only_page_is_quarantined_not_backfilled() -> None:
    observation = normalize_web_evidence_row(
        _license_row(
            timing_basis=TIMING_PAGE_METADATA,
            collector_observed_at=None,
            capture_mode=None,
        )
    )

    assert observation.provider_event_id == "证监许可〔2025〕689号"
    assert observation.document_url == (
        "https://www.csrc.gov.cn/csrc/c101857/c7552337/content.shtml"
    )
    assert observation.raw_response_sha256 == RAW_SHA256
    assert observation.source_released_at == datetime(2025, 4, 2, 15, 0, tzinfo=SHANGHAI)
    assert observation.vendor_first_available_at is None
    assert observation.time_quality is TimeQuality.DATE_ONLY_CONSERVATIVE
    assert observation.validation_status == "blocked_time_quality"
    assert observation.attributes["timestamp_precision"] == "date"
    assert market_available_at(observation) is None


@pytest.mark.parametrize("award_status", ["candidate", "intent", "shortlisted"])
def test_only_formal_contract_winner_is_accepted(award_status: str) -> None:
    observation = normalize_web_evidence_row(
        _contract_row(
            event_attributes={
                "award_status": award_status,
                "project_name": "数据平台建设项目",
                "awarding_entity": "某公共资源交易中心",
                "materiality_status": "material",
                "materiality_basis": "issuer_major_contract_disclosure",
            }
        )
    )

    assert observation.validation_status == "rejected"
    assert observation.attributes["award_stage"] == award_status
    assert "award_status" not in observation.attributes
    assert observation.document_sha256 == CONTENT_SHA256
    assert market_available_at(observation) is None


def test_small_or_unassessed_award_is_not_promoted_to_major_contract() -> None:
    observation = normalize_web_evidence_row(
        _contract_row(
            event_attributes={
                "award_stage": "formal_winner",
                "project_name": "普通采购项目",
                "counterparty": "某采购人",
                "materiality_status": "not_material",
                "materiality_basis": "exchange_materiality_review",
            }
        )
    )

    assert observation.validation_status == "rejected"
    assert market_available_at(observation) is None


def test_materiality_basis_is_a_fixed_policy_not_free_text() -> None:
    with pytest.raises(WebEvidenceError, match="materiality_basis must be one of"):
        normalize_web_evidence_row(
            _contract_row(
                event_attributes={
                    "award_stage": "formal_winner",
                    "project_name": "数据平台建设项目",
                    "counterparty": "某采购人",
                    "materiality_status": "material",
                    "materiality_basis": "model_says_so",
                }
            )
        )


@pytest.mark.parametrize("license_status", ["accepted", "pending", "proposed"])
def test_only_approved_license_is_accepted(license_status: str) -> None:
    observation = normalize_web_evidence_row(
        _license_row(
            event_attributes={
                "license_status": license_status,
                "license_name": "上市证券做市交易业务资格",
                "approving_authority": "中国证券监督管理委员会",
            }
        )
    )

    assert observation.validation_status == "rejected"
    assert observation.attributes["approval_status"] == license_status
    assert "license_status" not in observation.attributes
    assert market_available_at(observation) is None


def test_search_hit_is_a_discovery_object_with_no_observation_conversion() -> None:
    hit = SearchHit(
        search_provider="search.example",
        query="东方财富 做市 资格 核准",
        title="搜索结果标题",
        snippet="搜索摘要不是原始证据",
        url="https://example.test/discovery?id=1",
        retrieved_at=RETRIEVED_AT,
        raw_response_sha256="c" * 64,
        indexed_at_label="3 小时前",
    )

    assert not hasattr(hit, "to_observation")
    with pytest.raises(DiscoveryOnlyEvidenceError, match="discovery-only"):
        normalize_web_evidence_row(hit)


def test_search_index_time_never_becomes_market_time() -> None:
    observation = normalize_web_evidence_row(
        _contract_row(
            timing_basis=TIMING_SEARCH_INDEX,
            provider_license_status=None,
            vendor_first_available_at=None,
            search_indexed_at="2026-08-28 17:43:21+08:00",
        )
    )

    assert observation.source_released_at == datetime(2026, 8, 28, 17, 43, 20, tzinfo=SHANGHAI)
    assert observation.vendor_first_available_at is None
    assert observation.time_quality is TimeQuality.ESTIMATED_RESEARCH_ONLY
    assert observation.validation_status == "unverified"
    assert market_available_at(observation) is None


def test_minute_precision_archive_capture_remains_blocked() -> None:
    observation = normalize_web_evidence_row(
        _license_row(collector_observed_at="2026-08-29 10:15+08:00")
    )

    assert observation.vendor_first_available_at == datetime(2026, 8, 29, 10, 15, tzinfo=SHANGHAI)
    assert observation.time_quality is TimeQuality.VENDOR_OBSERVED
    assert observation.validation_status == "blocked_time_quality"
    assert observation.attributes["timestamp_precision"] == "minute"
    assert market_available_at(observation) is None


def test_historical_archive_import_cannot_claim_prospective_first_seen() -> None:
    observation = normalize_web_evidence_row(_license_row(capture_mode="historical_import"))

    assert observation.vendor_first_available_at == datetime(
        2026, 8, 29, 10, 15, 32, tzinfo=SHANGHAI
    )
    assert observation.validation_status == "unverified"
    assert market_available_at(observation) is None


def test_exact_page_metadata_is_still_unverified() -> None:
    observation = normalize_web_evidence_row(
        _contract_row(
            timing_basis=TIMING_PAGE_METADATA,
            provider_license_status=None,
            vendor_first_available_at=None,
        )
    )

    assert observation.vendor_first_available_at is None
    assert observation.time_quality is TimeQuality.EXACT
    assert observation.validation_status == "unverified"
    assert market_available_at(observation) is None


def test_non_authoritative_publisher_cannot_become_validated() -> None:
    observation = normalize_web_evidence_row(_contract_row(publisher_type="news_media"))

    assert observation.validation_status == "unverified"
    assert observation.attributes["publisher_authority"] == "secondary"
    assert market_available_at(observation) is None

    issuer_claim = normalize_web_evidence_row(_license_row(publisher_type="issuer"))
    assert issuer_claim.validation_status == "unverified"
    assert issuer_claim.attributes["publisher_authority"] == "secondary"


def test_pending_review_cannot_become_validated() -> None:
    observation = normalize_web_evidence_row(_contract_row(review_status="pending"))

    assert observation.validation_status == "unverified"
    assert observation.attributes["review_status"] == "pending"
    assert market_available_at(observation) is None


def test_runbook_field_aliases_normalize_to_one_internal_contract() -> None:
    observation = normalize_web_evidence_row(
        _license_row(
            timing_basis=None,
            first_seen_basis="prospective_collector",
            revision_type="original",
            source_released_at=None,
            source_published_at="2025-04-02",
            entity_mapping_status=None,
        )
    )

    assert observation.validation_status == "validated"
    assert observation.attributes["timing_basis"] == TIMING_PROSPECTIVE_ARCHIVE
    assert observation.attributes["revision_type"] == "initial"
    assert observation.attributes["entity_mapping_status"] == "exact"
    assert observation.attributes["source_timestamp_precision"] == "date"


@pytest.mark.parametrize(
    "row",
    [
        _contract_row(provider="web_archive"),
        _license_row(provider="eastmoney"),
    ],
)
def test_timing_basis_must_match_the_provider(row: dict[str, object]) -> None:
    with pytest.raises(WebEvidenceError, match=r"provider='web_archive'|web_archive cannot"):
        normalize_web_evidence_row(row)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"external_fact_id": ""}, "external_fact_id"),
        ({"content_sha256": "not-a-hash"}, "content_sha256"),
        ({"extractor_version": ""}, "extractor_version"),
        ({"extractor_id": ""}, "extractor_id"),
        ({"classifier_version": ""}, "classifier_version"),
        ({"classifier_id": ""}, "classifier_id"),
        ({"publisher_name": ""}, "publisher_name"),
        ({"publisher_type": "unknown"}, "publisher_type"),
        ({"entity_mapping_status": "fuzzy"}, "entity_mapping_status"),
        ({"entity_mapping_key": "600000.SH"}, "entity_mapping_key"),
        ({"entity_match_method": ""}, "entity_match_method"),
        ({"mapping_confidence": 0.99}, "mapping_confidence"),
        ({"review_status": ""}, "review_status"),
        ({"revision_id": ""}, "revision_id"),
        ({"evidence_span": None}, "evidence_span or evidence_sha256"),
        (
            {
                "event_attributes": {
                    "award_stage": "formal_winner",
                    "project_name": "数据平台建设项目",
                    "counterparty": "某公共资源交易中心",
                    "materiality_status": "material",
                }
            },
            "materiality_basis",
        ),
        ({"revision_type": "amendment", "revision_no": 0}, "revision_no"),
    ],
)
def test_missing_or_ambiguous_provenance_is_rejected_before_domain_entry(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(WebEvidenceError, match=message):
        normalize_web_evidence_row(_contract_row(**overrides))
