from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import duckdb
import pytest

from ashare_lab.adapters.event_sources import (
    EventCollectionRequest,
    EventCollectionResult,
    EventSourceCollection,
    build_event_acquisition_coverage,
    normalize_web_evidence_row,
)
from ashare_lab.adapters.event_sources.eastmoney import eastmoney_preparable_event_codes
from ashare_lab.adapters.market_data.event_snapshot import (
    EventSnapshotError,
    build_event_snapshot,
    build_no_event_required_snapshot,
    validate_persisted_acquisition_query_evidence,
)
from ashare_lab.domain.events import EventObservation
from ashare_lab.domain.market_data import TimeQuality
from ashare_lab.domain.shared import InstrumentId
from scripts.prepare_event_snapshot import DEFAULT_EVENT_CODES
from tests.unit.adapters.event_sources.query_evidence import eastmoney_query_evidence

SHANGHAI = ZoneInfo("Asia/Shanghai")
INSTRUMENT = InstrumentId("300059.SZ")
EVENT_CODE = "event.financial_results.annual_report"
CAPTURED_AT = datetime(2026, 8, 29, 14, 0, tzinfo=SHANGHAI)


def _acquisition_coverage(
    observations: tuple[EventObservation, ...] = (),
    *,
    start: date = date(2026, 1, 1),
    end: date = date(2026, 8, 29),
    requested_event_codes: tuple[str, ...] | None = None,
    primary_status: str | None = None,
) -> dict[str, object]:
    retrieved_at = observations[0].retrieved_at if observations else CAPTURED_AT
    if any(item.retrieved_at != retrieved_at for item in observations):
        raise AssertionError("fixture observations must share one retrieval clock")
    eastmoney = tuple(item for item in observations if item.provider == "eastmoney")
    supplemental = tuple(item for item in observations if item.provider != "eastmoney")
    status = primary_status or ("available" if eastmoney else "empty")
    codes = requested_event_codes or tuple(
        sorted({item.event_code for item in observations} or {EVENT_CODE})
    )
    source = EventSourceCollection(
        provider="eastmoney",
        status=status,
        observations=eastmoney if status == "available" else (),
        acquisition_evidence=(
            eastmoney_query_evidence(
                instrument_id=INSTRUMENT,
                start=start,
                end=end,
                event_codes=codes,
                total_hits=len(eastmoney),
                event_counts=Counter(item.event_code for item in eastmoney),
            )
            if status in {"available", "empty"}
            else None
        ),
        error_type="TimeoutError" if status == "failed" else None,
        error_message="timed out" if status == "failed" else None,
    )
    collection = EventCollectionResult(
        observations=source.observations,
        sources=(source,),
    )
    return build_event_acquisition_coverage(
        EventCollectionRequest(
            instrument_id=INSTRUMENT,
            start=start,
            end=end,
            retrieved_at=retrieved_at,
        ),
        collection,
        requested_event_codes=codes,
        supplemental_observations=supplemental,
    )


def _observation(
    provider: str,
    source_event_id: str,
    *,
    released_hour: int,
    validation_status: str = "validated",
    time_quality: TimeQuality = TimeQuality.VENDOR_OBSERVED,
) -> EventObservation:
    return EventObservation(
        provider=provider,
        provider_event_id=source_event_id,
        instrument_id=INSTRUMENT,
        event_code=EVENT_CODE,
        title="东方财富:2025年年度报告",
        occurred_at=None,
        source_released_at=datetime(
            2026,
            3,
            14,
            released_hour,
            30,
            0,
            tzinfo=SHANGHAI,
        ),
        vendor_first_available_at=datetime(
            2026,
            3,
            14,
            released_hour,
            31,
            0,
            tzinfo=SHANGHAI,
        ),
        retrieved_at=datetime(2026, 8, 29, 12, 0, tzinfo=SHANGHAI),
        time_quality=time_quality,
        document_url=f"https://example.test/{source_event_id}.pdf",
        raw_response_sha256=("a" if provider == "eastmoney" else "b") * 64,
        validation_status=validation_status,
        attributes={
            "report_type": "annual",
            "stat_date": "2025-12-31",
            "source_url": f"https://example.test/api/{source_event_id}",
        },
    )


def _web_contract_observation() -> EventObservation:
    return normalize_web_evidence_row(
        {
            "provider": "rqdata",
            "event_code": "event.contracts_orders.major_contract_won",
            "source_event_id": "rq-news-20260828-1",
            "external_fact_id": "CG-2026-001",
            "title": "东方财富正式中标数据平台建设项目",
            "canonical_url": "https://example.test/contracts/CG-2026-001",
            "raw_response_sha256": "b" * 64,
            "content_sha256": "c" * 64,
            "instrument_id": "300059.SZ",
            "entity_mapping_status": "exact",
            "entity_mapping_key": "300059.SZ",
            "entity_match_method": "exact_legal_name",
            "mapping_confidence": 1,
            "source_entity_id": "东方财富信息股份有限公司",
            "publisher_name": "某公共资源交易中心",
            "publisher_type": "public_procurement_platform",
            "extractor_id": "contract-fields",
            "extractor_version": "1.0.0",
            "classifier_id": "web-event-classifier",
            "classifier_version": "1.0.0",
            "revision_type": "initial",
            "revision_no": 0,
            "revision_id": "CG-2026-001:r0",
            "review_status": "accepted",
            "evidence_sha256": "d" * 64,
            "timing_basis": "licensed_vendor_first_available",
            "provider_license_status": "licensed",
            "source_released_at": "2026-08-28 17:43:20+08:00",
            "vendor_first_available_at": "2026-08-28 17:43:21+08:00",
            "event_attributes": {
                "award_stage": "formal_winner",
                "project_name": "数据平台建设项目",
                "counterparty": "某公共资源交易中心",
                "materiality_status": "material",
                "materiality_basis": "issuer_major_contract_disclosure",
            },
        },
        retrieved_at=CAPTURED_AT,
    )


def test_builds_deterministic_snapshot_without_using_retrieval_as_market_time(
    tmp_path: Path,
) -> None:
    observations = (
        _observation("ifind", "ifind-1", released_hour=18),
        _observation("eastmoney", "em-1", released_hour=20),
    )

    first = build_event_snapshot(
        observations=observations,
        acquisition_coverage=_acquisition_coverage(observations),
        output_root=tmp_path,
        captured_at=CAPTURED_AT,
    )
    second = build_event_snapshot(
        observations=tuple(reversed(observations)),
        acquisition_coverage=_acquisition_coverage(observations),
        output_root=tmp_path,
        captured_at=CAPTURED_AT,
    )

    assert first.snapshot_id == second.snapshot_id
    assert first.path == second.path
    manifest = json.loads((first.path / "event_snapshot_manifest.json").read_text(encoding="utf-8"))
    assert manifest["policy"]["providerPriority"] == [
        "eastmoney",
        "ifind",
        "rqdata",
        "tushare",
    ]
    assert (
        manifest["policy"]["eventLanePolicies"]["event.macro_policy_industry.license_approval"][
            "providerPriority"
        ][0]
        == "web_archive"
    )
    assert manifest["policy"]["retrievalClockUsedForReplay"] is False
    assert manifest["rowCounts"] == {
        "canonicalEvents": 1,
        "observations": 2,
        "quarantinedEvents": 0,
    }

    with duckdb.connect(database=":memory:") as connection:
        canonical = connection.execute(
            """
            SELECT provider, source_event_id, source_url,
                   replay_available_at, ingested_at, raw_response_sha256,
                   validation_status
            FROM read_parquet(?)
            """,
            [str(first.path / "events.parquet")],
        ).fetchone()
        raw_count = connection.execute(
            "SELECT count(*) FROM read_parquet(?)",
            [str(first.path / "event_observations.parquet")],
        ).fetchone()

    assert canonical == (
        "eastmoney",
        "em-1",
        "https://example.test/api/em-1",
        "2026-03-14T20:31:00+08:00",
        "2026-08-29T12:00:00+08:00",
        "a" * 64,
        "validated",
    )
    assert raw_count == (2,)


def test_no_event_required_snapshot_is_explicit_and_does_not_claim_a_zero_result_query(
    tmp_path: Path,
) -> None:
    result = build_no_event_required_snapshot(
        instrument_id=INSTRUMENT,
        start=date(2016, 1, 1),
        end=date(2026, 1, 1),
        output_root=tmp_path,
        captured_at=CAPTURED_AT,
    )

    manifest = json.loads(
        (result.path / "event_snapshot_manifest.json").read_text(encoding="utf-8")
    )
    coverage = manifest["acquisitionCoverage"]
    assert manifest["schemaVersion"] == "ashare-lab.event-snapshot.v2"
    assert manifest["eventRequirement"] == {
        "mode": "no_event_required",
        "eventDataAvailable": False,
        "acquisitionPerformed": False,
    }
    assert coverage["mode"] == "no_event_required"
    assert coverage["status"] == "not_required"
    assert coverage["acquisitionPerformed"] is False
    assert coverage["querySucceeded"] is False
    assert coverage["zeroResult"] is False
    assert coverage["emptyByConstruction"] is True
    assert coverage["requestedEventCodes"] == []
    assert coverage["requiredProviders"] == []
    assert coverage["sourceStatuses"] == []
    assert manifest["rowCounts"] == {
        "canonicalEvents": 0,
        "observations": 0,
        "quarantinedEvents": 0,
    }
    with duckdb.connect(database=":memory:") as connection:
        assert connection.execute(
            "SELECT count(*) FROM read_parquet(?)",
            [str(result.path / "events.parquet")],
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM read_parquet(?)",
            [str(result.path / "event_observations.parquet")],
        ).fetchone() == (0,)


def test_strict_snapshot_rejects_date_only_or_unverified_event(tmp_path: Path) -> None:
    blocked = _observation(
        "eastmoney",
        "em-date",
        released_hour=15,
        validation_status="blocked_time_quality",
        time_quality=TimeQuality.DATE_ONLY_CONSERVATIVE,
    )

    with pytest.raises(EventSnapshotError, match="validated second-level"):
        build_event_snapshot(
            observations=(blocked,),
            acquisition_coverage=_acquisition_coverage((blocked,)),
            output_root=tmp_path,
            captured_at=CAPTURED_AT,
        )


def test_snapshot_rejects_capture_clock_before_real_ingestion(tmp_path: Path) -> None:
    with pytest.raises(EventSnapshotError, match="cannot precede"):
        build_event_snapshot(
            observations=(_observation("eastmoney", "em-1", released_hour=20),),
            acquisition_coverage=_acquisition_coverage(
                (_observation("eastmoney", "em-1", released_hour=20),)
            ),
            output_root=tmp_path,
            captured_at=datetime(2026, 8, 1, 12, 0, tzinfo=SHANGHAI),
        )


def test_research_snapshot_persists_complete_web_fact_provenance(tmp_path: Path) -> None:
    observation = _web_contract_observation()

    result = build_event_snapshot(
        observations=(observation,),
        acquisition_coverage=_acquisition_coverage((observation,)),
        output_root=tmp_path,
        captured_at=CAPTURED_AT,
        strict_demo=False,
    )

    with duckdb.connect(database=":memory:") as connection:
        canonical = connection.execute(
            "SELECT external_fact_id, attributes_json FROM read_parquet(?)",
            [str(result.path / "events.parquet")],
        ).fetchone()
        raw = connection.execute(
            "SELECT external_fact_id FROM read_parquet(?)",
            [str(result.path / "event_observations.parquet")],
        ).fetchone()

    assert canonical is not None
    assert canonical[0] == "CG-2026-001"
    assert json.loads(canonical[1])["review_status"] == "accepted"
    assert raw == ("CG-2026-001",)


def test_strict_snapshot_accepts_authoritative_issuer_announcement_for_major_award(
    tmp_path: Path,
) -> None:
    secondary = _web_contract_observation()
    primary_without_evidence = replace(
        secondary,
        provider="eastmoney",
        provider_event_id="em-contract-1",
        attributes={
            "award_stage": "formal_winner",
            "project_name": "数据平台建设项目",
            "counterparty": "某公共资源交易中心",
            "materiality_status": "material",
            "materiality_basis": "issuer_major_contract_disclosure",
            "source_kind": "public_procurement_platform",
        },
    )

    result = build_event_snapshot(
        observations=(primary_without_evidence, secondary),
        acquisition_coverage=_acquisition_coverage((primary_without_evidence, secondary)),
        output_root=tmp_path,
        captured_at=CAPTURED_AT,
    )

    assert result.fusion.events[0].selected_provider == "eastmoney"
    assert result.fusion.events[0].event_code == ("event.contracts_orders.major_contract_won")


def test_strict_snapshot_requires_explicit_acquisition_coverage(tmp_path: Path) -> None:
    with pytest.raises(EventSnapshotError, match="acquisition coverage evidence"):
        build_event_snapshot(
            observations=(_observation("eastmoney", "em-1", released_hour=20),),
            output_root=tmp_path,
            captured_at=CAPTURED_AT,
        )


def test_successful_zero_result_query_is_a_replayable_strict_snapshot(tmp_path: Path) -> None:
    coverage = _acquisition_coverage(requested_event_codes=DEFAULT_EVENT_CODES)
    result = build_event_snapshot(
        observations=(),
        acquisition_coverage=coverage,
        output_root=tmp_path,
        captured_at=CAPTURED_AT,
    )

    manifest = json.loads(
        (result.path / "event_snapshot_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["rowCounts"]["canonicalEvents"] == 0
    assert manifest["acquisitionCoverage"]["status"] == "complete"
    assert manifest["acquisitionCoverage"]["zeroResult"] is True
    assert manifest["acquisitionCoverage"]["requestedEventCodes"] == sorted(DEFAULT_EVENT_CODES)
    lanes = manifest["acquisitionCoverage"]["coverageByEventCode"]
    assert set(lanes) == set(DEFAULT_EVENT_CODES)
    assert all(
        lane["status"] == "complete"
        and lane["querySucceeded"] is True
        and lane["zeroResult"] is True
        and lane["coverageBasis"]
        == "complete_announcement_interval_with_provider_column_classification"
        for lane in lanes.values()
    )
    eastmoney = manifest["acquisitionCoverage"]["providerQueryEvidence"]["eastmoney"]
    assert eastmoney["pagination"]["complete"] is True
    assert eastmoney["pagination"]["totalHits"] == 0
    assert eastmoney["pagination"]["pages"][0]["responseSha256"]


def test_strict_snapshot_rejects_tampered_page_response_evidence(tmp_path: Path) -> None:
    coverage = _acquisition_coverage()
    provider_evidence = cast(dict[str, Any], coverage["providerQueryEvidence"])
    eastmoney = cast(dict[str, Any], provider_evidence["eastmoney"])
    pagination = cast(dict[str, Any], eastmoney["pagination"])
    pages = cast(list[dict[str, Any]], pagination["pages"])
    pages[0]["responseSha256"] = "2" * 64

    with pytest.raises(EventSnapshotError, match="pagination evidence hash"):
        build_event_snapshot(
            observations=(),
            acquisition_coverage=coverage,
            output_root=tmp_path,
            captured_at=CAPTURED_AT,
        )


def test_all_preparable_codes_share_one_complete_zero_result_interval_contract(
    tmp_path: Path,
) -> None:
    codes = tuple(sorted(eastmoney_preparable_event_codes()))
    coverage = _acquisition_coverage(requested_event_codes=codes)

    normalized_provider_evidence = validate_persisted_acquisition_query_evidence(coverage)
    result = build_event_snapshot(
        observations=(),
        acquisition_coverage=coverage,
        output_root=tmp_path,
        captured_at=CAPTURED_AT,
    )

    lanes = cast(dict[str, dict[str, Any]], coverage["coverageByEventCode"])
    assert len(codes) == 68
    assert set(normalized_provider_evidence) == {"eastmoney"}
    assert set(lanes) == set(codes)
    assert all(
        lane["querySucceeded"] is True and lane["zeroResult"] is True for lane in lanes.values()
    )
    assert result.manifest["acquisitionCoverage"]["requestedEventCodes"] == list(codes)


def test_strict_snapshot_rejects_rehashed_inconsistent_classification_counts(
    tmp_path: Path,
) -> None:
    coverage = _acquisition_coverage()
    provider_evidence = cast(dict[str, Any], coverage["providerQueryEvidence"])
    eastmoney = cast(dict[str, Any], provider_evidence["eastmoney"])
    summary = cast(dict[str, Any], eastmoney["classificationSummary"])
    summary["classifiedRows"] = 1
    summary["unclassifiedRows"] = 0
    summary["rowsByEventCode"] = {EVENT_CODE: 1}
    summary.pop("evidenceSha256")
    summary["evidenceSha256"] = hashlib.sha256(
        json.dumps(
            summary,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    eastmoney.pop("evidenceSha256")
    eastmoney["evidenceSha256"] = hashlib.sha256(
        json.dumps(
            eastmoney,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    with pytest.raises(EventSnapshotError, match="classification summary"):
        build_event_snapshot(
            observations=(),
            acquisition_coverage=coverage,
            output_root=tmp_path,
            captured_at=CAPTURED_AT,
        )


def test_persisted_coverage_rejects_rehashed_fake_title_rule_column_recall() -> None:
    event_code = "event.dividends_corporate_actions.cash_dividend_proposal"
    coverage = _acquisition_coverage(requested_event_codes=(event_code,))
    provider_evidence = cast(dict[str, Any], coverage["providerQueryEvidence"])
    eastmoney = cast(dict[str, Any], provider_evidence["eastmoney"])
    by_code = cast(dict[str, dict[str, Any]], eastmoney["eventCodeCoverage"])
    by_code[event_code] = {
        "querySucceeded": True,
        "coverageBasis": "complete_announcement_interval_with_provider_column_classification",
        "providerColumnCodes": ["forged-title-rule-column"],
    }
    eastmoney.pop("evidenceSha256")
    eastmoney["evidenceSha256"] = hashlib.sha256(
        json.dumps(
            eastmoney,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    with pytest.raises(EventSnapshotError, match=r"classifier contract|titleRuleIds"):
        validate_persisted_acquisition_query_evidence(coverage)


def test_persisted_coverage_rejects_legacy_misleading_column_basis() -> None:
    coverage = _acquisition_coverage(requested_event_codes=(EVENT_CODE,))
    provider_evidence = cast(dict[str, Any], coverage["providerQueryEvidence"])
    eastmoney = cast(dict[str, Any], provider_evidence["eastmoney"])
    by_code = cast(dict[str, dict[str, Any]], eastmoney["eventCodeCoverage"])
    by_code[EVENT_CODE]["coverageBasis"] = "provider_column_interval_query"
    eastmoney.pop("evidenceSha256")
    eastmoney["evidenceSha256"] = hashlib.sha256(
        json.dumps(
            eastmoney,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    with pytest.raises(EventSnapshotError, match="fixed classifier contract"):
        validate_persisted_acquisition_query_evidence(coverage)


def test_strict_snapshot_rejects_failed_primary_coverage(tmp_path: Path) -> None:
    with pytest.raises(EventSnapshotError, match="primary Eastmoney"):
        build_event_snapshot(
            observations=(),
            acquisition_coverage=_acquisition_coverage(primary_status="failed"),
            output_root=tmp_path,
            captured_at=CAPTURED_AT,
        )


def test_license_observation_or_empty_eastmoney_query_does_not_prove_interval_coverage(
    tmp_path: Path,
) -> None:
    license_code = "event.macro_policy_industry.license_approval"
    coverage = _acquisition_coverage(
        requested_event_codes=(license_code,),
    )

    assert coverage["coverageByEventCode"][license_code]["requiredSources"] == ["web_archive"]
    assert coverage["coverageByEventCode"][license_code]["status"] == "incomplete"
    with pytest.raises(EventSnapshotError, match="every requested event code"):
        build_event_snapshot(
            observations=(),
            acquisition_coverage=coverage,
            output_root=tmp_path,
            captured_at=CAPTURED_AT,
        )
