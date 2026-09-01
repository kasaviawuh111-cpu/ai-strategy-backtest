from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ashare_lab.adapters.event_sources import (
    EventCollectionRequest,
    EventCollectionResult,
    EventSourceCollection,
    build_event_acquisition_coverage,
)
from ashare_lab.adapters.market_data import (
    CompositeSnapshotError,
    LocalParquetMarketDataRepository,
    MarketDataCapabilityError,
    SnapshotIntegrityError,
    compose_choice_event_snapshot,
)
from ashare_lab.adapters.market_data.choice_snapshot import (
    STRICT_CORPORATE_ACTION_CATEGORIES,
    STRICT_CORPORATE_ACTION_COVERAGE_SCOPE,
    TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
    ChoiceSnapshotSpec,
    DailySnapshotSource,
    build_choice_snapshot,
)
from ashare_lab.adapters.market_data.event_snapshot import (
    build_event_snapshot,
    build_no_event_required_snapshot,
)
from ashare_lab.domain.events import EventObservation
from ashare_lab.domain.market_data import Board, PriceBasis, TimeQuality
from ashare_lab.domain.shared import InstrumentId
from ashare_lab.ports.market_data import DataRequirements, DateRange
from scripts.prepare_event_snapshot import DEFAULT_EVENT_CODES
from tests.unit.adapters.event_sources.query_evidence import eastmoney_query_evidence
from tests.unit.adapters.session_reference_fixture import baostock_session_reference

SHANGHAI = ZoneInfo("Asia/Shanghai")
INSTRUMENT = InstrumentId("300059.SZ")
_ARROW: Any = pa
_PARQUET: Any = pq


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _republish_with_invalid_query_evidence(
    source: Path,
    output_root: Path,
    *,
    damage: str,
) -> Path:
    manifest = json.loads((source / "snapshot_manifest.json").read_text(encoding="utf-8"))
    coverage = manifest["eventAcquisitionCoverage"]
    evidence = coverage["providerQueryEvidence"]["eastmoney"]
    pagination = evidence["pagination"]
    if damage == "missing_page":
        pagination["pages"] = []
    elif damage == "bad_response_hash":
        pagination["pages"][0]["responseSha256"] = "not-a-sha256"
    elif damage == "title_rule_only":
        lane = evidence["eventCodeCoverage"]["event.financial_results.annual_report"]
        lane.update(
            {
                "querySucceeded": False,
                "coverageBasis": "deterministic_title_rule_no_semantic_recall_proof",
                "providerColumnCodes": [],
            }
        )
    else:  # pragma: no cover - a test helper programming error
        raise AssertionError(f"unsupported damage: {damage}")
    pagination.pop("evidenceSha256", None)
    pagination["evidenceSha256"] = _canonical_sha256(pagination)
    evidence.pop("evidenceSha256", None)
    evidence["evidenceSha256"] = _canonical_sha256(evidence)

    manifest.pop("snapshotId", None)
    digest = _canonical_sha256(manifest)
    manifest["snapshotId"] = f"composite:{digest}"
    destination = output_root / digest
    shutil.copytree(source, destination)
    (destination / "snapshot_manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return destination


def _choice_snapshot(
    root: Path,
    *,
    corporate_action_coverage: dict[str, object] | None = None,
    source: DailySnapshotSource | None = None,
):
    execution = [
        {
            "date": "2025-01-02",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "preclose": 9.8,
            "volume": 1000,
            "amount": 10_500,
            "highlimit": "否",
            "lowlimit": "否",
            "tradestatus": "正常交易",
        },
        {
            "date": "2025-01-03",
            "open": 10.6,
            "high": 11.2,
            "low": 10.1,
            "close": 11,
            "preclose": 10.5,
            "volume": 1200,
            "amount": 13_200,
            "highlimit": "否",
            "lowlimit": "否",
            "tradestatus": "正常交易",
        },
    ]
    signal = [
        {"date": "2025-01-02", "open": 20, "high": 22, "low": 18, "close": 21},
        {"date": "2025-01-03", "open": 21, "high": 23, "low": 20, "close": 22},
    ]
    session_rows, session_coverage = baostock_session_reference(
        symbol="300059.SZ",
        start=date(2025, 1, 2),
        end=date(2025, 1, 3),
        execution_rows=execution,
    )
    if source is not None and not source.limit_event_flags_available:
        execution = [
            {key: value for key, value in row.items() if key not in {"highlimit", "lowlimit"}}
            for row in execution
        ]
    build_kwargs: dict[str, object] = {}
    if source is not None:
        build_kwargs["source"] = source
    return build_choice_snapshot(
        spec=ChoiceSnapshotSpec(
            symbol="300059.SZ",
            start=date(2025, 1, 2),
            end=date(2025, 1, 3),
            listing_date=date(2010, 3, 19),
            board=Board.CHINEXT,
        ),
        execution_rows=execution,
        signal_rows=signal,
        market_calendar=[date(2025, 1, 2), date(2025, 1, 3)],
        raw_audit_payload={"responses": ["fixture"]},
        request_audit={"AdjustFlag": [1, 2]},
        prefix_stability={"status": "passed", "overlapRows": 1},
        output_root=root,
        captured_at=datetime(2025, 1, 4, tzinfo=UTC),
        sdk_archive_sha256="a" * 64,
        session_reference_rows=session_rows,
        session_reference_coverage=session_coverage,
        corporate_action_coverage=corporate_action_coverage
        or {
            "status": "complete",
            "querySucceeded": True,
            "provider": "fixture-source",
            "start": "2025-01-02",
            "end": "2025-01-03",
            "rawResponseSha256": "b" * 64,
            "coverageScope": STRICT_CORPORATE_ACTION_COVERAGE_SCOPE,
            "supportedCategories": list(STRICT_CORPORATE_ACTION_CATEGORIES),
            "unsupportedCategories": [],
        },
        **build_kwargs,
    )


def _push2_source() -> DailySnapshotSource:
    return DailySnapshotSource(
        schema_version=TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
        snapshot_prefix="technical",
        provider="Eastmoney Push2His public endpoint",
        dataset="stock_kline_day",
        account_scope="public_undocumented_research_demo",
        adjustment_field="eastmoneyFqt",
        execution_adjustment=0,
        signal_adjustment=2,
        acquisition_implementation="AKShare-compatible Push2His thin adapter",
        limit_event_flags_available=False,
        status_cross_check="BaoStock supplies and validates historical trading status",
        previous_close_cross_check="Push2 implied preclose equals BaoStock per date",
        limitations=("public undocumented endpoint; not authorized for production use",),
    )


def _mixed_corporate_action_coverage() -> dict[str, object]:
    counts = {category: 0 for category in STRICT_CORPORATE_ACTION_CATEGORIES}
    return {
        "status": "complete_mixed_mode",
        "querySucceeded": True,
        "provider": "Eastmoney public datasets",
        "start": "2025-01-02",
        "end": "2025-01-03",
        "rowCount": 0,
        "zeroResult": True,
        "rawResponseSha256": "d" * 64,
        "coverageScope": "all_categories_mixed_positive_and_negative_proof",
        "positiveCapableCategories": [
            "cash_dividend",
            "rights_issue",
            "share_distribution",
        ],
        "negativeProofCategories": ["reverse_split", "stock_split"],
        "unsupportedCategories": [],
        "strictEligibleUnderCurrentChoiceValidator": True,
        "categoryActionCounts": counts,
        "categoryCoverage": {
            "cash_dividend": {
                "status": "complete_for_filtered_dataset",
                "dataset": "RPT_SHAREBONUS_DET",
                "zeroResult": True,
            },
            "rights_issue": {
                "status": "complete_for_filtered_dataset",
                "dataset": "RPT_IPO_ALLOTMENT",
                "zeroResult": True,
            },
            "share_distribution": {
                "status": "complete_for_filtered_dataset",
                "dataset": "RPT_SHAREBONUS_DET",
                "zeroResult": True,
            },
            "reverse_split": {
                "status": "complete",
                "categoryMode": "complete_negative_proof",
                "dataset": "RPT_F10_EH_EQUITY",
                "candidateCount": 0,
                "zeroResult": True,
            },
            "stock_split": {
                "status": "complete",
                "categoryMode": "complete_negative_proof",
                "dataset": "RPT_F10_EH_EQUITY",
                "candidateCount": 0,
                "zeroResult": True,
            },
        },
        "negativeSplitProof": {
            "categoryMode": "complete_negative_proof",
            "queryScope": "full_instrument_history_filtered_locally_to_requested_interval",
            "start": "2025-01-02",
            "end": "2025-01-03",
            "scannedRows": 1,
            "recognizedChangeReasons": ["高管股份变动"],
            "stockSplitCandidates": 0,
            "reverseSplitCandidates": 0,
            "sourceDataset": "RPT_F10_EH_EQUITY",
            "sourceDeclaredCount": 1,
            "sourceTotalPages": 1,
            "sourceRawResponseSha256": "e" * 64,
        },
        "timeQuality": "date_only_conservative",
        "dateAvailabilityPolicy": "implementation notice date @ 15:00:00 Asia/Shanghai",
        "hashSemantics": "fixture raw body aggregate",
    }


def _event_snapshot(
    root: Path,
    *,
    coverage_start: date = date(2025, 1, 2),
    coverage_end: date = date(2025, 1, 3),
    include_event: bool = True,
    event_codes: tuple[str, ...] = ("event.financial_results.annual_report",),
    document_text: str | None = None,
    document_source_sha256: str = "c" * 64,
    document_attributes: dict[str, str | int] | None = None,
    extra_attributes: dict[str, str | int] | None = None,
):
    attributes: dict[str, str | int] = {
        "report_type": "annual",
        "stat_date": "2024-12-31",
        "source_url": "https://example.test/api/annual",
    }
    if document_text is not None:
        attributes.update(
            document_attributes
            or _complete_document_attributes(
                document_text,
                source_sha256=document_source_sha256,
            )
        )
    if extra_attributes is not None:
        attributes.update(extra_attributes)
    observation = EventObservation(
        provider="eastmoney",
        provider_event_id="AN202501020000000001",
        instrument_id=INSTRUMENT,
        event_code="event.financial_results.annual_report",
        title="东方财富:2024年年度报告",
        occurred_at=None,
        source_released_at=datetime(2025, 1, 2, 20, 0, tzinfo=SHANGHAI),
        vendor_first_available_at=datetime(2025, 1, 2, 20, 1, tzinfo=SHANGHAI),
        retrieved_at=datetime(2025, 1, 4, 8, 0, tzinfo=SHANGHAI),
        time_quality=TimeQuality.VENDOR_OBSERVED,
        document_url="https://example.test/annual.pdf",
        document_sha256="c" * 64 if document_text is not None else None,
        raw_response_sha256="b" * 64,
        validation_status="validated",
        attributes=attributes,
    )
    observations = (observation,) if include_event else ()
    source = EventSourceCollection(
        provider="eastmoney",
        status="available" if observations else "empty",
        observations=observations,
        acquisition_evidence=eastmoney_query_evidence(
            instrument_id=INSTRUMENT,
            start=coverage_start,
            end=coverage_end,
            event_codes=event_codes,
            total_hits=len(observations),
        ),
    )
    acquisition_coverage = build_event_acquisition_coverage(
        EventCollectionRequest(
            instrument_id=INSTRUMENT,
            start=coverage_start,
            end=coverage_end,
            retrieved_at=datetime(2025, 1, 4, 8, 0, tzinfo=SHANGHAI),
        ),
        EventCollectionResult(observations=observations, sources=(source,)),
        requested_event_codes=event_codes,
    )
    return build_event_snapshot(
        observations=observations,
        acquisition_coverage=acquisition_coverage,
        output_root=root,
        captured_at=datetime(2025, 1, 4, 9, 0, tzinfo=SHANGHAI),
    )


def _complete_document_attributes(
    text: str,
    *,
    source_sha256: str = "c" * 64,
) -> dict[str, str | int]:
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    non_whitespace_count = sum(not character.isspace() for character in text)
    return {
        "document_text": text,
        "document_text_sha256": text_sha256,
        "document_text_source_sha256": source_sha256,
        "document_text_quality": "complete_text_layer",
        "document_text_normalization": "unicode_nfkc_lf_strip.v1",
        "document_text_page_count": 1,
        "document_text_provider_page_count": 1,
        "document_text_extracted_page_count": 1,
        "document_text_empty_page_count": 0,
        "document_version_role": "initial_complete",
        "document_text_scope": "complete_primary_document",
        "document_text_source_format": "notice_content",
        "document_text_extractor": "eastmoney_notice_content",
        "document_text_extractor_version": "1",
        "document_text_character_count": len(text),
        "document_text_non_whitespace_character_count": non_whitespace_count,
        "document_text_pages_json": json.dumps(
            [
                {
                    "pageNumber": 1,
                    "text": text,
                    "textSha256": text_sha256,
                    "characterCount": len(text),
                    "nonWhitespaceCharacterCount": non_whitespace_count,
                }
            ],
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
    }


def _damage_document_attributes(
    attributes: dict[str, object],
    *,
    damage: str,
) -> None:
    if damage == "missing_source_format":
        attributes.pop("document_text_source_format")
    elif damage == "extractor_identity":
        attributes["document_text_extractor"] = "unsupported_extractor"
    elif damage == "text_hash":
        attributes["document_text"] = str(attributes["document_text"]) + " tampered"
    elif damage == "source_hash":
        attributes["document_text_source_sha256"] = "d" * 64
    else:
        pages = cast(
            list[dict[str, object]], json.loads(str(attributes["document_text_pages_json"]))
        )
        page = pages[0]
        if damage == "page_number":
            page["pageNumber"] = 2
        elif damage == "page_hash":
            page["textSha256"] = "d" * 64
        elif damage == "page_character_count":
            page["characterCount"] = int(cast(int, page["characterCount"])) + 1
        elif damage == "document_character_count":
            attributes["document_text_character_count"] = (
                int(cast(int, attributes["document_text_character_count"])) + 1
            )
        elif damage == "rebuild":
            page_text = str(page["text"]) + " rebuilt differently"
            page["text"] = page_text
            page["textSha256"] = hashlib.sha256(page_text.encode("utf-8")).hexdigest()
            page["characterCount"] = len(page_text)
            page["nonWhitespaceCharacterCount"] = sum(
                not character.isspace() for character in page_text
            )
        else:  # pragma: no cover - a test helper programming error
            raise AssertionError(f"unsupported document damage: {damage}")
        attributes["document_text_pages_json"] = json.dumps(
            pages,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )


def _republish_with_document_damage(
    source: Path,
    output_root: Path,
    *,
    damage: str,
) -> Path:
    mutable = output_root / "mutable"
    shutil.copytree(source, mutable)
    events_path = mutable / "events.parquet"
    table = _PARQUET.read_table(events_path)
    rows = cast(list[dict[str, object]], table.to_pylist())
    attributes = cast(dict[str, object], json.loads(cast(str, rows[0]["attributes_json"])))
    _damage_document_attributes(attributes, damage=damage)
    rows[0]["attributes_json"] = json.dumps(
        attributes,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    _PARQUET.write_table(_ARROW.Table.from_pylist(rows, schema=table.schema), events_path)

    manifest_path = mutable / "snapshot_manifest.json"
    manifest = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
    files = cast(dict[str, dict[str, object]], manifest["files"])
    files["events.parquet"] = {
        "bytes": events_path.stat().st_size,
        "sha256": hashlib.sha256(events_path.read_bytes()).hexdigest(),
    }
    manifest.pop("snapshotId")
    digest = _canonical_sha256(manifest)
    manifest["snapshotId"] = f"composite:{digest}"
    manifest_path.write_text(
        json.dumps(
            manifest,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    destination = output_root / digest
    mutable.rename(destination)
    return destination


def _composite(tmp_path: Path):
    choice = _choice_snapshot(tmp_path / "choice")
    events = _event_snapshot(tmp_path / "events")
    return compose_choice_event_snapshot(
        choice_snapshot_path=choice.path,
        event_snapshot_path=events.path,
        output_root=tmp_path / "composite",
        composed_at=datetime(2025, 1, 4, 10, 0, tzinfo=SHANGHAI),
    )


def _technical_only_composite(tmp_path: Path):
    choice = _choice_snapshot(tmp_path / "choice-technical")
    events = build_no_event_required_snapshot(
        instrument_id=INSTRUMENT,
        start=date(2025, 1, 2),
        end=date(2025, 1, 3),
        output_root=tmp_path / "events-technical",
        captured_at=datetime(2025, 1, 4, 9, 0, tzinfo=SHANGHAI),
    )
    return compose_choice_event_snapshot(
        choice_snapshot_path=choice.path,
        event_snapshot_path=events.path,
        output_root=tmp_path / "composite-technical",
        composed_at=datetime(2025, 1, 4, 10, 0, tzinfo=SHANGHAI),
    )


def test_technical_only_composite_keeps_event_v2_without_advertising_event_capability(
    tmp_path: Path,
) -> None:
    result = _technical_only_composite(tmp_path)

    manifest = json.loads((result.path / "snapshot_manifest.json").read_text(encoding="utf-8"))
    assert manifest["schemaVersion"] == "ashare-lab.composite-research-snapshot.v2"
    assert manifest["capabilities"]["technicalDaily"] == "validated_for_demo"
    assert manifest["capabilities"]["events"] == "not_required"
    assert manifest["eventRequirement"] == {
        "mode": "no_event_required",
        "eventDataAvailable": False,
        "acquisitionPerformed": False,
    }
    assert manifest["eventAcquisitionCoverage"]["status"] == "not_required"
    assert manifest["eventAcquisitionCoverage"]["requestedEventCodes"] == []
    assert manifest["rowCounts"]["canonicalEvents"] == 0
    assert manifest["rowCounts"]["eventObservations"] == 0
    assert (result.path / "events.parquet").is_file()
    assert (result.path / "event_observations.parquet").is_file()


def test_strict_pin_accepts_a_technical_only_composite(tmp_path: Path) -> None:
    result = _technical_only_composite(tmp_path)
    repository = LocalParquetMarketDataRepository(result.path, profile="composite_snapshot")

    snapshot = repository.pin_strict_composite_snapshot()

    assert snapshot.producer_snapshot_id == result.snapshot_id
    assert repository.strict_composite_event_codes() == frozenset()


def test_composite_accepts_allowlisted_provider_neutral_technical_source(
    tmp_path: Path,
) -> None:
    technical = _choice_snapshot(
        tmp_path / "push2-technical",
        source=_push2_source(),
    )
    events = build_no_event_required_snapshot(
        instrument_id=INSTRUMENT,
        start=date(2025, 1, 2),
        end=date(2025, 1, 3),
        output_root=tmp_path / "events-push2-technical",
        captured_at=datetime(2025, 1, 4, 9, 0, tzinfo=SHANGHAI),
    )

    result = compose_choice_event_snapshot(
        choice_snapshot_path=technical.path,
        event_snapshot_path=events.path,
        output_root=tmp_path / "composite-push2-technical",
        composed_at=datetime(2025, 1, 4, 10, 0, tzinfo=SHANGHAI),
    )

    assert (result.path / "source/technical_snapshot_manifest.json").is_file()
    assert not (result.path / "source/choice_snapshot_manifest.json").exists()
    assert "technical" in cast(dict[str, object], result.manifest["baseSnapshots"])
    technical_source = cast(dict[str, object], result.manifest["technicalSource"])
    assert technical_source["provider"] == "Eastmoney Push2His public endpoint"

    repository = LocalParquetMarketDataRepository(result.path, profile="composite_snapshot")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    snapshot = repository.pin_snapshot(
        DataRequirements(
            instruments=(INSTRUMENT,),
            datasets=("daily_ohlcv",),
        ),
        period,
    )
    assert repository.load_daily_bars(snapshot, INSTRUMENT, period)


def test_composite_reuses_choice_validation_for_mixed_corporate_action_coverage(
    tmp_path: Path,
) -> None:
    choice = _choice_snapshot(
        tmp_path / "choice-mixed",
        corporate_action_coverage=_mixed_corporate_action_coverage(),
    )
    events = build_no_event_required_snapshot(
        instrument_id=INSTRUMENT,
        start=date(2025, 1, 2),
        end=date(2025, 1, 3),
        output_root=tmp_path / "events-mixed",
        captured_at=datetime(2025, 1, 4, 9, 0, tzinfo=SHANGHAI),
    )

    result = compose_choice_event_snapshot(
        choice_snapshot_path=choice.path,
        event_snapshot_path=events.path,
        output_root=tmp_path / "composite-mixed",
        composed_at=datetime(2025, 1, 4, 10, 0, tzinfo=SHANGHAI),
    )

    coverage = cast(dict[str, object], result.manifest["corporateActionCoverage"])
    assert coverage["status"] == "complete_mixed_mode"
    assert coverage["categoryActionCounts"] == {
        category: 0 for category in STRICT_CORPORATE_ACTION_CATEGORIES
    }


def test_composite_profile_pins_technical_event_and_provenance_files(
    tmp_path: Path,
) -> None:
    result = _composite(tmp_path)
    repository = LocalParquetMarketDataRepository(result.path, profile="composite_snapshot")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    snapshot = repository.pin_snapshot(
        DataRequirements(
            instruments=(INSTRUMENT,),
            datasets=("daily_ohlcv", "events"),
            event_codes=("event.financial_results.annual_report",),
        ),
        period,
    )

    assert snapshot.schema_version == "local-parquet.market-data.v3"
    assert snapshot.producer_schema_version == "ashare-lab.composite-research-snapshot.v2"
    assert snapshot.producer_snapshot_id == result.snapshot_id

    execution = repository.load_daily_bars(snapshot, INSTRUMENT, period)
    signal = repository.load_signal_bars(snapshot, INSTRUMENT, period)
    events = repository.load_events(snapshot, INSTRUMENT, period)
    sessions = repository.load_sessions(snapshot, INSTRUMENT, period)

    assert execution[0].price_basis is PriceBasis.UNADJUSTED
    assert signal[0].price_basis is PriceBasis.BACK_ADJUSTED
    assert len(events) == 1
    assert [item.session_date for item in sessions] == [date(2025, 1, 2), date(2025, 1, 3)]
    assert all(item.instrument_id == INSTRUMENT for item in sessions)
    event = events[0]
    assert event.provider == "eastmoney"
    assert event.source_event_id == "AN202501020000000001"
    assert event.source_url == "https://example.test/api/annual"
    assert event.validation_status == "validated"
    assert event.ingested_at == datetime(2025, 1, 4, 8, 0, tzinfo=SHANGHAI)
    assert event.available_at == datetime(2025, 1, 2, 20, 1, tzinfo=SHANGHAI)
    assert (result.path / "source/choice_snapshot_manifest.json").is_file()
    assert (result.path / "source/event_snapshot_manifest.json").is_file()


def test_document_metric_pin_rejects_old_event_snapshot_without_frozen_text(
    tmp_path: Path,
) -> None:
    result = _composite(tmp_path)
    repository = LocalParquetMarketDataRepository(result.path, profile="composite_snapshot")

    with pytest.raises(MarketDataCapabilityError, match="document text is not frozen"):
        repository.pin_snapshot(
            DataRequirements(
                instruments=(INSTRUMENT,),
                datasets=("daily_ohlcv", "events"),
                event_codes=("event.financial_results.annual_report",),
                needs_event_document_text=True,
            ),
            DateRange(date(2025, 1, 2), date(2025, 1, 3)),
        )


def test_document_scope_metadata_does_not_poison_generic_event_snapshot(
    tmp_path: Path,
) -> None:
    choice = _choice_snapshot(tmp_path / "choice")
    events = _event_snapshot(
        tmp_path / "events",
        extra_attributes={
            "document_version_role": "initial_complete",
            "document_text_scope": "complete_primary_document",
        },
    )
    result = compose_choice_event_snapshot(
        choice_snapshot_path=choice.path,
        event_snapshot_path=events.path,
        output_root=tmp_path / "composite",
        composed_at=datetime(2025, 1, 4, 10, 0, tzinfo=SHANGHAI),
    )
    repository = LocalParquetMarketDataRepository(result.path, profile="composite_snapshot")

    repository.pin_strict_composite_snapshot()
    assert repository.strict_composite_event_codes() == {"event.financial_results.annual_report"}
    assert repository.strict_composite_event_document_text_codes() == frozenset()

    with pytest.raises(MarketDataCapabilityError, match="document text is not frozen"):
        repository.pin_snapshot(
            DataRequirements(
                instruments=(INSTRUMENT,),
                datasets=("daily_ohlcv", "events"),
                event_codes=("event.financial_results.annual_report",),
                needs_event_document_text=True,
            ),
            DateRange(date(2025, 1, 2), date(2025, 1, 3)),
        )


def test_document_metric_pin_accepts_complete_content_addressed_text(
    tmp_path: Path,
) -> None:
    choice = _choice_snapshot(tmp_path / "choice")
    events = _event_snapshot(tmp_path / "events", document_text="AI AI AI AI AI AI")
    result = compose_choice_event_snapshot(
        choice_snapshot_path=choice.path,
        event_snapshot_path=events.path,
        output_root=tmp_path / "composite",
        composed_at=datetime(2025, 1, 4, 10, 0, tzinfo=SHANGHAI),
    )
    repository = LocalParquetMarketDataRepository(result.path, profile="composite_snapshot")

    snapshot = repository.pin_snapshot(
        DataRequirements(
            instruments=(INSTRUMENT,),
            datasets=("daily_ohlcv", "events"),
            event_codes=("event.financial_results.annual_report",),
            needs_event_document_text=True,
        ),
        DateRange(date(2025, 1, 2), date(2025, 1, 3)),
    )

    assert (
        len(
            repository.load_events(
                snapshot,
                INSTRUMENT,
                DateRange(date(2025, 1, 2), date(2025, 1, 3)),
            )
        )
        == 1
    )


def test_strict_document_text_codes_exclude_a_generic_event_only_snapshot(
    tmp_path: Path,
) -> None:
    result = _composite(tmp_path)
    repository = LocalParquetMarketDataRepository(result.path, profile="composite_snapshot")

    assert repository.strict_composite_event_codes() == {"event.financial_results.annual_report"}
    assert repository.strict_composite_event_document_text_codes() == frozenset()


def test_strict_composite_technical_and_event_requests_share_one_slice_identity(
    tmp_path: Path,
) -> None:
    result = _composite(tmp_path)
    repository = LocalParquetMarketDataRepository(result.path, profile="composite_snapshot")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))

    technical = repository.pin_snapshot(
        DataRequirements(
            instruments=(INSTRUMENT,),
            datasets=("daily_ohlcv", "corporate_actions"),
        ),
        period,
    )
    event = repository.pin_snapshot(
        DataRequirements(
            instruments=(INSTRUMENT,),
            datasets=("daily_ohlcv", "corporate_actions", "events"),
            event_codes=("event.financial_results.annual_report",),
        ),
        period,
    )

    assert technical.snapshot_id == event.snapshot_id
    assert technical.checksum == event.checksum
    assert technical.producer_snapshot_id == event.producer_snapshot_id == result.snapshot_id


def test_strict_document_text_codes_require_complete_artifacts_for_every_matching_row(
    tmp_path: Path,
) -> None:
    choice = _choice_snapshot(tmp_path / "choice")
    events = _event_snapshot(tmp_path / "events", document_text="AI AI AI AI AI AI")
    result = compose_choice_event_snapshot(
        choice_snapshot_path=choice.path,
        event_snapshot_path=events.path,
        output_root=tmp_path / "composite",
        composed_at=datetime(2025, 1, 4, 10, 0, tzinfo=SHANGHAI),
    )
    repository = LocalParquetMarketDataRepository(result.path, profile="composite_snapshot")

    assert repository.strict_composite_event_document_text_codes() == {
        "event.financial_results.annual_report"
    }


def test_composite_rejects_text_not_tied_to_frozen_source_document(
    tmp_path: Path,
) -> None:
    choice = _choice_snapshot(tmp_path / "choice")
    events = _event_snapshot(
        tmp_path / "events",
        document_text="AI AI AI AI AI AI",
        document_source_sha256="d" * 64,
    )

    with pytest.raises(CompositeSnapshotError, match="frozen source document"):
        compose_choice_event_snapshot(
            choice_snapshot_path=choice.path,
            event_snapshot_path=events.path,
            output_root=tmp_path / "composite",
            composed_at=datetime(2025, 1, 4, 10, 0, tzinfo=SHANGHAI),
        )


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("missing_source_format", "document_text_source_format.*missing"),
        ("page_hash", "page text hash does not match"),
        ("rebuild", "does not rebuild"),
    ],
)
def test_composite_rejects_malformed_document_artifact_before_publish(
    tmp_path: Path,
    damage: str,
    message: str,
) -> None:
    text = "AI AI AI AI AI AI"
    attributes = cast(dict[str, object], _complete_document_attributes(text))
    _damage_document_attributes(attributes, damage=damage)
    choice = _choice_snapshot(tmp_path / "choice")
    events = _event_snapshot(
        tmp_path / "events",
        document_text=text,
        document_attributes=cast(dict[str, str | int], attributes),
    )

    with pytest.raises(CompositeSnapshotError, match=message):
        compose_choice_event_snapshot(
            choice_snapshot_path=choice.path,
            event_snapshot_path=events.path,
            output_root=tmp_path / "composite",
            composed_at=datetime(2025, 1, 4, 10, 0, tzinfo=SHANGHAI),
        )


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("missing_source_format", "document_text_source_format.*missing"),
        ("extractor_identity", "extractor identity"),
        ("text_hash", "text hash does not match"),
        ("source_hash", "frozen source document"),
        ("page_number", "page numbers are not contiguous"),
        ("page_hash", "page text hash does not match"),
        ("page_character_count", "page character count"),
        ("document_character_count", "document text character count"),
        ("rebuild", "does not rebuild"),
    ],
)
def test_document_metric_pin_rejects_resealed_document_tampering_during_preflight(
    tmp_path: Path,
    damage: str,
    message: str,
) -> None:
    choice = _choice_snapshot(tmp_path / "choice")
    events = _event_snapshot(
        tmp_path / "events",
        document_text="AI AI AI AI AI AI",
    )
    valid = compose_choice_event_snapshot(
        choice_snapshot_path=choice.path,
        event_snapshot_path=events.path,
        output_root=tmp_path / "composite",
        composed_at=datetime(2025, 1, 4, 10, 0, tzinfo=SHANGHAI),
    )
    invalid_path = _republish_with_document_damage(
        valid.path,
        tmp_path / "invalid",
        damage=damage,
    )
    repository = LocalParquetMarketDataRepository(
        invalid_path,
        profile="composite_snapshot",
    )

    with pytest.raises(MarketDataCapabilityError, match=message):
        repository.pin_strict_composite_snapshot()


def test_observation_file_change_invalidates_pinned_run_snapshot(tmp_path: Path) -> None:
    result = _composite(tmp_path)
    repository = LocalParquetMarketDataRepository(result.path, profile="composite_snapshot")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    snapshot = repository.pin_snapshot(
        DataRequirements(instruments=(INSTRUMENT,), datasets=("daily_ohlcv", "events")),
        period,
    )
    (result.path / "event_observations.parquet").write_bytes(b"changed")

    with pytest.raises(SnapshotIntegrityError, match="changed"):
        repository.load_events(snapshot, INSTRUMENT, period)


def test_single_event_cannot_claim_a_wider_choice_range(tmp_path: Path) -> None:
    choice = _choice_snapshot(tmp_path / "choice")
    events = _event_snapshot(
        tmp_path / "events",
        coverage_start=date(2025, 1, 2),
        coverage_end=date(2025, 1, 2),
    )

    with pytest.raises(CompositeSnapshotError, match="does not span"):
        compose_choice_event_snapshot(
            choice_snapshot_path=choice.path,
            event_snapshot_path=events.path,
            output_root=tmp_path / "composite",
            composed_at=datetime(2025, 1, 4, 10, 0, tzinfo=SHANGHAI),
        )


def test_complete_zero_event_coverage_can_be_composed_and_replayed(tmp_path: Path) -> None:
    choice = _choice_snapshot(tmp_path / "choice")
    events = _event_snapshot(
        tmp_path / "events",
        include_event=False,
        event_codes=DEFAULT_EVENT_CODES,
    )
    result = compose_choice_event_snapshot(
        choice_snapshot_path=choice.path,
        event_snapshot_path=events.path,
        output_root=tmp_path / "composite",
        composed_at=datetime(2025, 1, 4, 10, 0, tzinfo=SHANGHAI),
    )
    repository = LocalParquetMarketDataRepository(result.path, profile="composite_snapshot")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    snapshot = repository.pin_snapshot(
        DataRequirements(
            instruments=(INSTRUMENT,),
            datasets=("daily_ohlcv", "events"),
            event_codes=DEFAULT_EVENT_CODES,
        ),
        period,
    )

    assert repository.load_events(snapshot, INSTRUMENT, period) == ()
    coverage = cast(dict[str, Any], result.manifest["eventAcquisitionCoverage"])
    assert coverage["zeroResult"] is True
    assert coverage["requestedEventCodes"] == sorted(DEFAULT_EVENT_CODES)
    lanes = cast(dict[str, dict[str, Any]], coverage["coverageByEventCode"])
    assert set(lanes) == set(DEFAULT_EVENT_CODES)
    assert all(
        lane["status"] == "complete"
        and lane["querySucceeded"] is True
        and lane["zeroResult"] is True
        for lane in lanes.values()
    )


def test_pin_snapshot_rejects_period_or_event_code_outside_acquisition_coverage(
    tmp_path: Path,
) -> None:
    result = _composite(tmp_path)
    repository = LocalParquetMarketDataRepository(result.path, profile="composite_snapshot")

    with pytest.raises(MarketDataCapabilityError, match=r"period.*outside"):
        repository.pin_snapshot(
            DataRequirements(
                instruments=(INSTRUMENT,),
                datasets=("daily_ohlcv", "events"),
                event_codes=("event.financial_results.annual_report",),
            ),
            DateRange(date(2025, 1, 1), date(2025, 1, 3)),
        )

    with pytest.raises(MarketDataCapabilityError, match=r"event codes.*outside"):
        repository.pin_snapshot(
            DataRequirements(
                instruments=(INSTRUMENT,),
                datasets=("daily_ohlcv", "events"),
                event_codes=("event.macro_policy_industry.license_approval",),
            ),
            DateRange(date(2025, 1, 2), date(2025, 1, 3)),
        )


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("missing_page", "page evidence is incomplete"),
        ("bad_response_hash", "page evidence is invalid"),
        ("title_rule_only", "lacks interval query evidence"),
    ],
)
def test_strict_pin_rejects_incomplete_or_unproven_provider_query_evidence(
    tmp_path: Path,
    damage: str,
    message: str,
) -> None:
    valid = _composite(tmp_path / "valid")
    invalid = _republish_with_invalid_query_evidence(
        valid.path,
        tmp_path / "invalid",
        damage=damage,
    )
    repository = LocalParquetMarketDataRepository(invalid, profile="composite_snapshot")

    with pytest.raises(SnapshotIntegrityError, match=message):
        repository.pin_strict_composite_snapshot()
