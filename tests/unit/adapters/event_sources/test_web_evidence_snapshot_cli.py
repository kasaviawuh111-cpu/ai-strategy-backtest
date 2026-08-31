from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.domain.shared import InstrumentId
from scripts.prepare_event_snapshot import _load_web_evidence

SHANGHAI = ZoneInfo("Asia/Shanghai")
RETRIEVED_AT = datetime(2026, 8, 29, 14, 0, tzinfo=SHANGHAI)


def _row() -> dict[str, object]:
    return {
        "provider": "web_archive",
        "event_code": "event.macro_policy_industry.license_approval",
        "source_event_id": "csrc-c7552337",
        "external_fact_id": "证监许可〔2025〕689号",
        "title": "关于核准东方财富证券上市证券做市交易业务资格的批复",
        "canonical_url": "https://www.csrc.gov.cn/csrc/c101857/c7552337/content.shtml",
        "raw_response_sha256": ("a1b5d42a7fea1f166fb1488ae9ce5f4993ff7eefab2e408e56378d4eaaa4012c"),
        "content_sha256": ("a1b5d42a7fea1f166fb1488ae9ce5f4993ff7eefab2e408e56378d4eaaa4012c"),
        "instrument_id": "300059.SZ",
        "entity_mapping_status": "exact",
        "entity_mapping_key": "300059.SZ",
        "entity_match_method": "point_in_time_controlled_subsidiary",
        "mapping_confidence": 1,
        "source_entity_id": "东方财富证券股份有限公司",
        "publisher_name": "中国证券监督管理委员会",
        "publisher_type": "regulator",
        "extractor_id": "regulatory-license-fields",
        "extractor_version": "1.0.0",
        "classifier_id": "web-event-classifier",
        "classifier_version": "1.0.0",
        "revision_type": "initial",
        "revision_no": 0,
        "revision_id": "证监许可〔2025〕689号:r0",
        "review_status": "accepted",
        "evidence_span": "核准东方财富证券上市证券做市交易业务资格。",
        "timing_basis": "page_metadata_only",
        "source_released_at": "2025-04-02",
        "event_attributes": {
            "approval_status": "approved",
            "license_type": "上市证券做市交易业务资格",
            "regulator": "中国证券监督管理委员会",
        },
    }


def test_web_evidence_json_enters_the_snapshot_input_with_a_pinned_clock(
    tmp_path: Path,
) -> None:
    path = tmp_path / "web-evidence.json"
    path.write_text(json.dumps([_row()], ensure_ascii=False), encoding="utf-8")

    observations, report = _load_web_evidence(
        (path,),
        allowed_codes=frozenset({"event.macro_policy_industry.license_approval"}),
        instrument_id=InstrumentId("300059.SZ"),
        retrieved_at=RETRIEVED_AT,
    )

    assert len(observations) == 1
    assert observations[0].retrieved_at == RETRIEVED_AT
    assert observations[0].external_fact_id == "证监许可〔2025〕689号"
    assert report == {
        "files": 1,
        "inputRows": 1,
        "acceptedRows": 1,
        "byProvider": {"web_archive": 1},
        "byValidationStatus": {"blocked_time_quality": 1},
    }


def test_web_evidence_json_cannot_silently_change_the_requested_stock(
    tmp_path: Path,
) -> None:
    row = _row()
    row["instrument_id"] = "600519.SH"
    row["entity_mapping_key"] = "600519.SH"
    path = tmp_path / "wrong-stock.json"
    path.write_text(json.dumps([row], ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match=r"targets 600519\.SH, expected 300059\.SZ"):
        _load_web_evidence(
            (path,),
            allowed_codes=frozenset({"event.macro_policy_industry.license_approval"}),
            instrument_id=InstrumentId("300059.SZ"),
            retrieved_at=RETRIEVED_AT,
        )
