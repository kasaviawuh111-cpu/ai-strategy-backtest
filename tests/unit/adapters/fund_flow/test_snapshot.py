from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import httpx
import pytest

from ashare_lab.adapters.fund_flow import (
    EastmoneyFundFlowResearchSource,
    FundFlowSnapshotError,
    build_fund_flow_research_snapshot,
    load_fund_flow_research_snapshot,
)
from ashare_lab.domain.shared import InstrumentId

SHANGHAI = ZoneInfo("Asia/Shanghai")
REQUEST_STARTED_AT = datetime(2026, 8, 29, 16, 29, 59, tzinfo=SHANGHAI)
RESPONSE_RECEIVED_AT = datetime(2026, 8, 29, 16, 30, tzinfo=SHANGHAI)


def _clock(*values: datetime) -> Callable[[], datetime]:
    iterator = iter(values)
    return lambda: next(iterator)


def _collection():
    body = json.dumps(
        {
            "rc": 0,
            "data": {
                "code": "300059",
                "market": 0,
                "name": "东方财富",
                "klines": [
                    "2026-08-28,100,-70,-30,60,40,1,-0.7,-0.3,0.6,0.4,25.18,2.03",
                    "2026-08-27,80,-55,-25,50,30,0.8,-0.55,-0.25,0.5,0.3,24.68,1.11",
                ],
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, request=request)

    with EastmoneyFundFlowResearchSource(
        transport=httpx.MockTransport(handler),
        clock=_clock(REQUEST_STARTED_AT, RESPONSE_RECEIVED_AT),
    ) as source:
        return source.fetch(instrument_id=InstrumentId("300059.SZ"))


def _load_payload(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _publish_rehashed(tmp_path: Path, payload: dict[str, object]) -> Path:
    body = {key: value for key, value in payload.items() if key != "snapshotId"}
    digest = hashlib.sha256(_canonical_bytes(body)).hexdigest()
    payload["snapshotId"] = f"fund-flow:{digest}"
    path = tmp_path / f"{digest}.json"
    path.write_bytes(_canonical_bytes(payload))
    return path


def _nested_object(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload[key]
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _rows(payload: dict[str, object]) -> list[dict[str, object]]:
    value = payload["rows"]
    assert isinstance(value, list)
    return cast(list[dict[str, object]], value)


def test_snapshot_is_atomic_content_addressed_and_replay_verifiable(tmp_path: Path) -> None:
    collection = _collection()
    first = build_fund_flow_research_snapshot(collection=collection, output_root=tmp_path)
    second = build_fund_flow_research_snapshot(collection=collection, output_root=tmp_path)

    assert first.snapshot_id == second.snapshot_id
    assert first.path == second.path
    assert list(tmp_path.glob("*.tmp")) == []
    payload = load_fund_flow_research_snapshot(first.path)
    assert payload["schemaVersion"] == "ashare-lab.fund-flow-research.v2"
    assert payload["researchOnly"] is True
    assert payload["frequency"] == "1d"
    assert payload["retrievedAt"] == RESPONSE_RECEIVED_AT.isoformat()

    request = payload["request"]
    assert isinstance(request, dict)
    assert request["requestStartedAt"] == REQUEST_STARTED_AT.isoformat()
    assert request["responseReceivedAt"] == RESPONSE_RECEIVED_AT.isoformat()
    assert request["rawWireHashSemantics"] == "sha256_of_exact_http_response_body_bytes"
    assert request["rawPayloadCanonicalization"] == "utf8_json_sorted_keys_compact_no_nan"

    assert payload["methodology"] == {
        "mainIdentityValidation": "f52 ~= f55 + f56",
        "methodologyUnknown": True,
        "notOrderBookGroundTruth": True,
        "providerLabelsPreserved": True,
    }
    assert payload["historicalAvailability"] == {
        "earliestExecution": "not_applicable",
        "eligibleForHistoricalBacktest": False,
        "historicalKnownAt": RESPONSE_RECEIVED_AT.isoformat(),
        "reason": "historical_source_availability_time_unknown",
    }
    assert payload["collectionSessionPolicy"] == {
        "isHistoricalAvailabilityTimestamp": False,
        "scope": "current_session_response_collection_only",
        "stabilityMarkerLocalTime": "16:05:00",
        "timezone": "Asia/Shanghai",
    }
    runtime = payload["runtimePolicy"]
    assert isinstance(runtime, dict)
    assert runtime["networkFetchAllowedDuringBacktest"] is False
    assert runtime["historicalMinuteDataIncluded"] is False

    coverage = payload["coverage"]
    assert isinstance(coverage, dict)
    assert coverage["configuredRequestLimitMaximumRows"] == 120
    assert coverage["historicalCoverageGuaranteed"] is False
    assert coverage["serviceLevelAgreement"] is None
    assert "publicInterfaceVerifiedApproxTradingDays" not in coverage

    rows = payload["rows"]
    assert isinstance(rows, list)
    assert [row["tradeDate"] for row in rows if isinstance(row, dict)] == [
        "2026-08-27",
        "2026-08-28",
    ]
    for row in rows:
        assert isinstance(row, dict)
        assert row["historicalKnownAt"] == payload["retrievedAt"]
        assert row["eligibleForHistoricalBacktest"] is False
        assert "policyAvailableAt" not in row
    assert payload["sourcePayload"] == collection.raw_payload


def test_tampered_snapshot_is_rejected(tmp_path: Path) -> None:
    result = build_fund_flow_research_snapshot(collection=_collection(), output_root=tmp_path)
    payload = _load_payload(result.path)
    _rows(payload)[0]["mainNetInflowCny"] = "999"
    result.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FundFlowSnapshotError, match="content hash"):
        load_fund_flow_research_snapshot(result.path)


def test_snapshot_filename_must_match_content_digest(tmp_path: Path) -> None:
    result = build_fund_flow_research_snapshot(collection=_collection(), output_root=tmp_path)
    wrong_name = tmp_path / "renamed.json"
    wrong_name.write_bytes(result.path.read_bytes())

    with pytest.raises(FundFlowSnapshotError, match="filename"):
        load_fund_flow_research_snapshot(wrong_name)


def test_raw_payload_canonical_hash_is_independently_verified(tmp_path: Path) -> None:
    result = build_fund_flow_research_snapshot(collection=_collection(), output_root=tmp_path)
    payload = _load_payload(result.path)
    source_payload = _nested_object(payload, "sourcePayload")
    source_data = _nested_object(source_payload, "data")
    source_data["name"] = "被篡改"
    rehashed = _publish_rehashed(tmp_path, payload)

    with pytest.raises(FundFlowSnapshotError, match="raw payload canonical hash"):
        load_fund_flow_research_snapshot(rehashed)


@pytest.mark.parametrize(
    ("object_name", "field_name", "replacement", "message"),
    [
        ("root", "researchOnly", False, "research-only"),
        ("methodology", "methodologyUnknown", False, "methodology must remain unknown"),
        (
            "runtimePolicy",
            "networkFetchAllowedDuringBacktest",
            True,
            "prohibit runtime network fetches",
        ),
        ("runtimePolicy", "snapshotRequired", False, "must require a snapshot"),
        (
            "historicalAvailability",
            "eligibleForHistoricalBacktest",
            True,
            "not eligible for historical backtests",
        ),
        (
            "historicalAvailability",
            "earliestExecution",
            "next_trading_session",
            "not_applicable",
        ),
        (
            "collectionSessionPolicy",
            "isHistoricalAvailabilityTimestamp",
            True,
            "cannot be a historical availability timestamp",
        ),
    ],
)
def test_semantic_guardrails_fail_closed_after_outer_hash_is_recomputed(
    tmp_path: Path,
    object_name: str,
    field_name: str,
    replacement: object,
    message: str,
) -> None:
    result = build_fund_flow_research_snapshot(collection=_collection(), output_root=tmp_path)
    payload = _load_payload(result.path)
    target = payload if object_name == "root" else _nested_object(payload, object_name)
    target[field_name] = replacement
    rehashed = _publish_rehashed(tmp_path, payload)

    with pytest.raises(FundFlowSnapshotError, match=message):
        load_fund_flow_research_snapshot(rehashed)


@pytest.mark.parametrize("mutation", ["empty", "unsorted", "duplicate"])
def test_rows_must_be_nonempty_sorted_and_unique(tmp_path: Path, mutation: str) -> None:
    result = build_fund_flow_research_snapshot(collection=_collection(), output_root=tmp_path)
    payload = _load_payload(result.path)
    rows = _rows(payload)
    if mutation == "empty":
        payload["rows"] = []
        message = "non-empty"
    elif mutation == "unsorted":
        rows.reverse()
        message = "sorted with unique dates"
    else:
        rows[1]["tradeDate"] = rows[0]["tradeDate"]
        message = "sorted with unique dates"
    rehashed = _publish_rehashed(tmp_path, payload)

    with pytest.raises(FundFlowSnapshotError, match=message):
        load_fund_flow_research_snapshot(rehashed)
