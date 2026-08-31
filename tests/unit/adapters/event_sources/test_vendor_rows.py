from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.adapters.event_sources.ifind import (
    BLOCKED_TIME_QUALITY,
    VALIDATED,
    IFindEventSourceAdapter,
    VendorEventRowError,
    normalize_ifind_row,
)
from ashare_lab.adapters.event_sources.rqdata import normalize_rqdata_row
from ashare_lab.adapters.event_sources.tushare import normalize_tushare_row
from ashare_lab.domain.market_data import TimeQuality
from ashare_lab.domain.shared import InstrumentId

SHANGHAI = ZoneInfo("Asia/Shanghai")
EVENT_CODE = "event.financial_results.annual_report"
RETRIEVED_AT = datetime(2025, 3, 19, 8, 0, tzinfo=SHANGHAI)


def _ifind_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "thscode": "300059.SZ",
        "seq": "ifind-annual-2024",
        "reportTitle": "东方财富：2024年年度报告",
        "ctime": "2025-03-18T10:31:06+00:00",
        "pdfURL": "https://example.test/ifind/annual.pdf",
        "reportDate": "2025-03-19",
    }
    row.update(overrides)
    return row


def test_ifind_maps_ctime_and_preserves_auditable_raw_fields() -> None:
    observation = normalize_ifind_row(
        _ifind_row(),
        event_code=EVENT_CODE,
        retrieved_at=RETRIEVED_AT,
    )

    assert observation.provider == "ifind"
    assert observation.provider_event_id == "ifind-annual-2024"
    assert observation.instrument_id == InstrumentId("300059.SZ")
    assert observation.source_released_at == datetime(
        2025,
        3,
        18,
        18,
        31,
        6,
        tzinfo=SHANGHAI,
    )
    assert observation.vendor_first_available_at is None
    assert observation.time_quality is TimeQuality.EXACT
    assert observation.validation_status == VALIDATED
    assert observation.document_url == "https://example.test/ifind/annual.pdf"
    assert observation.attributes["ctime"] == "2025-03-18T10:31:06+00:00"
    assert observation.attributes["reportDate"] == "2025-03-19"
    assert observation.raw_response_sha256 is not None
    assert len(observation.raw_response_sha256) == 64


@pytest.mark.parametrize(
    "raw_time",
    ["2025-03-18", "2025-03-18 00:00:00", "2025-03-18 12:00:00"],
)
def test_ifind_date_or_placeholder_is_conservative_and_blocked(raw_time: str) -> None:
    observation = normalize_ifind_row(
        _ifind_row(ctime=raw_time),
        event_code=EVENT_CODE,
        retrieved_at=RETRIEVED_AT,
    )

    assert observation.source_released_at == datetime(
        2025,
        3,
        18,
        15,
        0,
        tzinfo=SHANGHAI,
    )
    assert observation.time_quality is TimeQuality.DATE_ONLY_CONSERVATIVE
    assert observation.validation_status == BLOCKED_TIME_QUALITY


def test_rqdata_keeps_source_date_and_exact_vendor_ingestion_separate() -> None:
    row = {
        "order_book_id": "600000.XSHG",
        "title": "浦发银行：2024年年度报告",
        "info_date": "2025-03-18",
        "create_tm": datetime(2025, 3, 18, 16, 43, 3),
        "announcement_link": "https://example.test/rqdata/annual.pdf",
        "category": "定期报告",
    }

    observation = normalize_rqdata_row(
        row,
        event_code=EVENT_CODE,
        retrieved_at=RETRIEVED_AT,
    )

    assert observation.provider == "rqdata"
    assert observation.provider_event_id == row["announcement_link"]
    assert observation.instrument_id == InstrumentId("600000.SH")
    assert observation.source_released_at == datetime(
        2025,
        3,
        18,
        15,
        0,
        tzinfo=SHANGHAI,
    )
    assert observation.vendor_first_available_at == datetime(
        2025,
        3,
        18,
        16,
        43,
        3,
        tzinfo=SHANGHAI,
    )
    assert observation.time_quality is TimeQuality.VENDOR_OBSERVED
    assert observation.validation_status == VALIDATED
    assert observation.attributes["create_tm"] == "2025-03-18T16:43:03"


def test_tushare_maps_second_level_rec_time_to_shanghai() -> None:
    row = {
        "ts_code": "300059.SZ",
        "title": "东方财富：年度报告",
        "url": "https://example.test/tushare/annual.pdf",
        "ann_date": "20250319",
        "rec_time": "2025/03/18 18:32:06",
    }

    observation = normalize_tushare_row(
        row,
        event_code=EVENT_CODE,
        retrieved_at=RETRIEVED_AT.astimezone(UTC),
    )

    assert observation.provider == "tushare"
    assert observation.provider_event_id == row["url"]
    assert observation.source_released_at == datetime(
        2025,
        3,
        18,
        18,
        32,
        6,
        tzinfo=SHANGHAI,
    )
    assert observation.retrieved_at == RETRIEVED_AT
    assert observation.time_quality is TimeQuality.EXACT
    assert observation.validation_status == VALIDATED


def test_callable_adapter_needs_no_vendor_sdk() -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fetch_rows(*args: object, **kwargs: object):
        calls.append((args, kwargs))
        return [_ifind_row()]

    adapter = IFindEventSourceAdapter(fetch_rows)
    observations = adapter.fetch(
        "300059.SZ",
        event_code=EVENT_CODE,
        retrieved_at=RETRIEVED_AT,
        start="2025-01-01",
    )

    assert len(observations) == 1
    assert calls == [(("300059.SZ",), {"start": "2025-01-01"})]


def test_raw_response_digest_is_stable_across_mapping_order() -> None:
    original = _ifind_row()
    reversed_row = dict(reversed(tuple(original.items())))

    first = normalize_ifind_row(
        original,
        event_code=EVENT_CODE,
        retrieved_at=RETRIEVED_AT,
    )
    second = normalize_ifind_row(
        reversed_row,
        event_code=EVENT_CODE,
        retrieved_at=RETRIEVED_AT,
    )

    assert first.raw_response_sha256 == second.raw_response_sha256


@pytest.mark.parametrize(
    ("normalizer", "row", "missing_field"),
    [
        (
            normalize_ifind_row,
            {
                "thscode": "300059.SZ",
                "seq": "ifind-1",
                "reportTitle": "公告",
            },
            "ctime",
        ),
        (
            normalize_rqdata_row,
            {
                "order_book_id": "300059.XSHE",
                "title": "公告",
                "info_date": "2025-03-18",
                "announcement_link": "https://example.test/rq.pdf",
            },
            "create_tm",
        ),
        (
            normalize_tushare_row,
            {
                "ts_code": "300059.SZ",
                "title": "公告",
                "url": "https://example.test/ts.pdf",
            },
            "rec_time",
        ),
    ],
)
def test_missing_critical_vendor_time_fails_explicitly(
    normalizer,
    row: dict[str, object],
    missing_field: str,
) -> None:
    with pytest.raises(VendorEventRowError, match=missing_field):
        normalizer(
            row,
            event_code=EVENT_CODE,
            retrieved_at=RETRIEVED_AT,
        )


def test_missing_or_naive_ingestion_time_is_rejected() -> None:
    with pytest.raises(VendorEventRowError, match=r"retrieved_at|ingested_at"):
        normalize_ifind_row(_ifind_row(), event_code=EVENT_CODE)

    with pytest.raises(VendorEventRowError, match="explicit timezone"):
        normalize_ifind_row(
            _ifind_row(),
            event_code=EVENT_CODE,
            retrieved_at=datetime(2025, 3, 19, 8, 0),
        )
