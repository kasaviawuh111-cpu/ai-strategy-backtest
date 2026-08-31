"""Atomically publish replayable, research-only daily fund-flow JSON snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import cast

from .eastmoney import (
    DAILY_FIELD_CODES,
    PUBLIC_REQUEST_LIMIT_MAX_ROWS,
    FundFlowCollection,
    JsonObject,
    JsonValue,
)

FUND_FLOW_SNAPSHOT_SCHEMA_VERSION = "ashare-lab.fund-flow-research.v2"
_RAW_WIRE_HASH_SEMANTICS = "sha256_of_exact_http_response_body_bytes"
_RAW_PAYLOAD_CANONICALIZATION = "utf8_json_sorted_keys_compact_no_nan"
_FIELD_MAP: JsonObject = {
    "f51": {"name": "tradeDate", "unit": "date"},
    "f52": {"name": "mainNetInflow", "unit": "CNY"},
    "f53": {"name": "smallNetInflow", "unit": "CNY"},
    "f54": {"name": "mediumNetInflow", "unit": "CNY"},
    "f55": {"name": "largeNetInflow", "unit": "CNY"},
    "f56": {"name": "extraLargeNetInflow", "unit": "CNY"},
    "f57": {"name": "mainNetInflowPct", "unit": "percent"},
    "f58": {"name": "smallNetInflowPct", "unit": "percent"},
    "f59": {"name": "mediumNetInflowPct", "unit": "percent"},
    "f60": {"name": "largeNetInflowPct", "unit": "percent"},
    "f61": {"name": "extraLargeNetInflowPct", "unit": "percent"},
    "f62": {"name": "close", "unit": "CNY_per_share"},
    "f63": {"name": "changePct", "unit": "percent"},
}


class FundFlowSnapshotError(RuntimeError):
    """A research snapshot cannot be published or replay-verified."""


@dataclass(frozen=True, slots=True)
class FundFlowSnapshotResult:
    snapshot_id: str
    path: Path
    payload: Mapping[str, JsonValue]


def build_fund_flow_research_snapshot(
    *,
    collection: FundFlowCollection,
    output_root: Path,
) -> FundFlowSnapshotResult:
    """Build one content-addressed JSON file and publish it with ``os.replace``."""

    if not collection.rows:
        raise FundFlowSnapshotError("a fund-flow snapshot requires at least one row")
    body = _snapshot_body(collection)
    digest = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    snapshot_id = f"fund-flow:{digest}"
    payload: JsonObject = {"snapshotId": snapshot_id, **body}
    _validate_payload_semantics(payload)
    encoded = _canonical_json_bytes(payload)

    root = output_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{digest}.json"
    if destination.exists():
        if destination.read_bytes() != encoded:
            raise FundFlowSnapshotError(
                f"snapshot destination contains different content: {destination}"
            )
        load_fund_flow_research_snapshot(destination)
        return FundFlowSnapshotResult(snapshot_id, destination, payload)

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".fund-flow-",
            suffix=".tmp",
            dir=root,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    load_fund_flow_research_snapshot(destination)
    return FundFlowSnapshotResult(snapshot_id, destination, payload)


def load_fund_flow_research_snapshot(path: Path) -> JsonObject:
    """Load a snapshot and verify its schema and content-addressed identity."""

    try:
        decoded: object = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FundFlowSnapshotError(f"fund-flow snapshot is unreadable: {path}") from exc
    if not isinstance(decoded, dict):
        raise FundFlowSnapshotError("fund-flow snapshot root must be an object")
    raw_payload = cast(dict[object, object], decoded)
    if any(not isinstance(key, str) for key in raw_payload):
        raise FundFlowSnapshotError("fund-flow snapshot root must be an object")
    payload = cast(JsonObject, decoded)
    if payload.get("schemaVersion") != FUND_FLOW_SNAPSHOT_SCHEMA_VERSION:
        raise FundFlowSnapshotError("fund-flow snapshot schemaVersion is unsupported")
    if payload.get("researchOnly") is not True:
        raise FundFlowSnapshotError("fund-flow snapshot must remain research-only")
    snapshot_id = payload.get("snapshotId")
    if not isinstance(snapshot_id, str) or not snapshot_id.startswith("fund-flow:"):
        raise FundFlowSnapshotError("fund-flow snapshotId is invalid")
    body = {key: value for key, value in payload.items() if key != "snapshotId"}
    digest = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    expected = f"fund-flow:{digest}"
    if snapshot_id != expected:
        raise FundFlowSnapshotError("fund-flow snapshot content hash does not match snapshotId")
    if path.name != f"{digest}.json":
        raise FundFlowSnapshotError("fund-flow snapshot filename does not match content digest")
    _validate_payload_semantics(payload)
    return payload


def _snapshot_body(collection: FundFlowCollection) -> JsonObject:
    retrieved_at = collection.retrieved_at.isoformat()
    rows: list[JsonValue] = []
    for row in collection.rows:
        rows.append(
            {
                "tradeDate": row.trade_date.isoformat(),
                "historicalKnownAt": retrieved_at,
                "eligibleForHistoricalBacktest": False,
                "mainNetInflowCny": str(row.main_net_inflow_cny),
                "smallNetInflowCny": str(row.small_net_inflow_cny),
                "mediumNetInflowCny": str(row.medium_net_inflow_cny),
                "largeNetInflowCny": str(row.large_net_inflow_cny),
                "extraLargeNetInflowCny": str(row.extra_large_net_inflow_cny),
                "mainNetInflowPct": str(row.main_net_inflow_pct),
                "smallNetInflowPct": str(row.small_net_inflow_pct),
                "mediumNetInflowPct": str(row.medium_net_inflow_pct),
                "largeNetInflowPct": str(row.large_net_inflow_pct),
                "extraLargeNetInflowPct": str(row.extra_large_net_inflow_pct),
                "closeCny": str(row.close_cny),
                "changePct": str(row.change_pct),
                "sourceRow": row.source_row,
            }
        )
    first = collection.rows[0].trade_date.isoformat()
    last = collection.rows[-1].trade_date.isoformat()
    return {
        "schemaVersion": FUND_FLOW_SNAPSHOT_SCHEMA_VERSION,
        "researchOnly": True,
        "provider": collection.provider_name,
        "dataset": collection.dataset_name,
        "instrumentId": collection.instrument_id.value,
        "frequency": "1d",
        "retrievedAt": retrieved_at,
        "request": {
            "method": "GET",
            "url": collection.request_url,
            "params": dict(sorted(collection.request_params.items())),
            "requestStartedAt": collection.request_started_at.isoformat(),
            "responseReceivedAt": collection.response_received_at.isoformat(),
            "rawWireSha256": collection.raw_wire_sha256,
            "rawWireHashSemantics": _RAW_WIRE_HASH_SEMANTICS,
            "rawPayloadCanonicalSha256": collection.raw_payload_canonical_sha256,
            "rawPayloadCanonicalization": _RAW_PAYLOAD_CANONICALIZATION,
        },
        "methodology": {
            "methodologyUnknown": True,
            "providerLabelsPreserved": True,
            "mainIdentityValidation": "f52 ~= f55 + f56",
            "notOrderBookGroundTruth": True,
        },
        "historicalAvailability": {
            "historicalKnownAt": retrieved_at,
            "eligibleForHistoricalBacktest": False,
            "earliestExecution": "not_applicable",
            "reason": "historical_source_availability_time_unknown",
        },
        "collectionSessionPolicy": {
            "timezone": "Asia/Shanghai",
            "stabilityMarkerLocalTime": "16:05:00",
            "scope": "current_session_response_collection_only",
            "isHistoricalAvailabilityTimestamp": False,
        },
        "runtimePolicy": {
            "networkFetchAllowedDuringBacktest": False,
            "snapshotRequired": True,
            "historicalMinuteDataIncluded": False,
        },
        "coverage": {
            "rowCount": len(collection.rows),
            "firstTradeDate": first,
            "lastTradeDate": last,
            "requestedLimit": collection.requested_limit,
            "configuredRequestLimitMaximumRows": PUBLIC_REQUEST_LIMIT_MAX_ROWS,
            "historicalCoverageGuaranteed": False,
            "serviceLevelAgreement": None,
            "scope": "request_configuration_only_not_observed_coverage",
        },
        "fieldOrder": list(DAILY_FIELD_CODES),
        "fieldMap": _FIELD_MAP,
        "sourcePayload": collection.raw_payload,
        "rows": rows,
    }


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FundFlowSnapshotError("fund-flow snapshot is not canonical JSON") from exc


def _validate_payload_semantics(payload: JsonObject) -> None:
    retrieved_text = _required_text(payload.get("retrievedAt"), "retrievedAt")
    retrieved_at = _aware_datetime(retrieved_text, "retrievedAt")

    request = _required_object(payload.get("request"), "request")
    request_started_at = _aware_datetime(
        _required_text(request.get("requestStartedAt"), "request.requestStartedAt"),
        "request.requestStartedAt",
    )
    response_received_text = _required_text(
        request.get("responseReceivedAt"), "request.responseReceivedAt"
    )
    response_received_at = _aware_datetime(
        response_received_text,
        "request.responseReceivedAt",
    )
    if response_received_at < request_started_at:
        raise FundFlowSnapshotError("responseReceivedAt precedes requestStartedAt")
    if response_received_at != retrieved_at:
        raise FundFlowSnapshotError("retrievedAt must equal responseReceivedAt")

    _require_sha256(request.get("rawWireSha256"), "request.rawWireSha256")
    if request.get("rawWireHashSemantics") != _RAW_WIRE_HASH_SEMANTICS:
        raise FundFlowSnapshotError("raw wire hash semantics are unsupported")
    raw_payload = _required_object(payload.get("sourcePayload"), "sourcePayload")
    raw_payload_hash = _required_text(
        request.get("rawPayloadCanonicalSha256"),
        "request.rawPayloadCanonicalSha256",
    )
    _require_sha256(raw_payload_hash, "request.rawPayloadCanonicalSha256")
    if request.get("rawPayloadCanonicalization") != _RAW_PAYLOAD_CANONICALIZATION:
        raise FundFlowSnapshotError("raw payload canonicalization is unsupported")
    expected_raw_payload_hash = hashlib.sha256(_canonical_json_bytes(raw_payload)).hexdigest()
    if raw_payload_hash != expected_raw_payload_hash:
        raise FundFlowSnapshotError("raw payload canonical hash does not match sourcePayload")

    methodology = _required_object(payload.get("methodology"), "methodology")
    if methodology.get("methodologyUnknown") is not True:
        raise FundFlowSnapshotError("fund-flow methodology must remain unknown")

    runtime = _required_object(payload.get("runtimePolicy"), "runtimePolicy")
    if runtime.get("networkFetchAllowedDuringBacktest") is not False:
        raise FundFlowSnapshotError("fund-flow snapshot must prohibit runtime network fetches")
    if runtime.get("snapshotRequired") is not True:
        raise FundFlowSnapshotError("fund-flow runtime must require a snapshot")

    historical = _required_object(payload.get("historicalAvailability"), "historicalAvailability")
    if historical.get("historicalKnownAt") != retrieved_text:
        raise FundFlowSnapshotError("historicalKnownAt must equal retrievedAt")
    if historical.get("eligibleForHistoricalBacktest") is not False:
        raise FundFlowSnapshotError("fund-flow rows are not eligible for historical backtests")
    if historical.get("earliestExecution") != "not_applicable":
        raise FundFlowSnapshotError("earliestExecution must remain not_applicable")

    session_policy = _required_object(
        payload.get("collectionSessionPolicy"), "collectionSessionPolicy"
    )
    if session_policy.get("isHistoricalAvailabilityTimestamp") is not False:
        raise FundFlowSnapshotError("session marker cannot be a historical availability timestamp")
    if session_policy.get("scope") != "current_session_response_collection_only":
        raise FundFlowSnapshotError("session marker scope is unsupported")

    rows_value = payload.get("rows")
    if not isinstance(rows_value, list) or not rows_value:
        raise FundFlowSnapshotError("fund-flow snapshot rows must be a non-empty array")
    rows = cast(list[JsonValue], rows_value)
    dates: list[date] = []
    for index, row_value in enumerate(rows):
        row = _required_object(row_value, f"rows[{index}]")
        trade_date_text = _required_text(row.get("tradeDate"), f"rows[{index}].tradeDate")
        try:
            trade_date = date.fromisoformat(trade_date_text)
        except ValueError as exc:
            raise FundFlowSnapshotError(f"rows[{index}].tradeDate is invalid") from exc
        if row.get("historicalKnownAt") != retrieved_text:
            raise FundFlowSnapshotError(f"rows[{index}].historicalKnownAt must equal retrievedAt")
        if row.get("eligibleForHistoricalBacktest") is not False:
            raise FundFlowSnapshotError(
                f"rows[{index}] must not be eligible for historical backtests"
            )
        dates.append(trade_date)
    if dates != sorted(set(dates)):
        raise FundFlowSnapshotError("fund-flow snapshot rows must be sorted with unique dates")

    coverage = _required_object(payload.get("coverage"), "coverage")
    if coverage.get("rowCount") != len(rows):
        raise FundFlowSnapshotError("coverage.rowCount does not match rows")
    if coverage.get("firstTradeDate") != dates[0].isoformat():
        raise FundFlowSnapshotError("coverage.firstTradeDate does not match rows")
    if coverage.get("lastTradeDate") != dates[-1].isoformat():
        raise FundFlowSnapshotError("coverage.lastTradeDate does not match rows")
    if coverage.get("configuredRequestLimitMaximumRows") != PUBLIC_REQUEST_LIMIT_MAX_ROWS:
        raise FundFlowSnapshotError("configured request limit metadata is invalid")
    if coverage.get("historicalCoverageGuaranteed") is not False:
        raise FundFlowSnapshotError("historical coverage must not be represented as guaranteed")
    if coverage.get("serviceLevelAgreement") is not None:
        raise FundFlowSnapshotError("public endpoint has no recorded service-level agreement")


def _required_object(value: JsonValue | None, field_name: str) -> JsonObject:
    if not isinstance(value, dict):
        raise FundFlowSnapshotError(f"{field_name} must be an object")
    return value


def _required_text(value: JsonValue | None, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise FundFlowSnapshotError(f"{field_name} must be non-empty text")
    return value


def _aware_datetime(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FundFlowSnapshotError(f"{field_name} must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FundFlowSnapshotError(f"{field_name} must include a timezone")
    return parsed


def _require_sha256(value: JsonValue | None, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FundFlowSnapshotError(f"{field_name} must be a lowercase SHA-256 digest")
