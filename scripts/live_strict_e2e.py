#!/usr/bin/env python3
"""Run the fixed technical and event acceptance cases against a live strict API."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from ashare_lab.domain.strategy import canonical_hash

_CLEAN_SHA = re.compile(r"^[0-9a-f]{40}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_START = "2021-08-06"
_END = "2026-08-06"
_INITIAL_CASH = 1_000_000
_PIN_SCHEMA = "local-parquet.market-data.v3"
_COMPOSITE_SCHEMA = "ashare-lab.composite-research-snapshot.v2"
_COMPOSITE_ID = re.compile(r"^composite:[0-9a-f]{64}$")
_SLICE_ID = re.compile(r"^snapshot:[0-9a-f]{64}$")
_REQUIRED_EXECUTION_ASSUMPTIONS = (
    "benchmark_policy",
    "commission_rate",
    "corporate_action_policy",
    "dividend_tax_policy",
    "entry_signal_validity_policy",
    "fee_schedule_version",
    "market_rule_version",
    "minimum_commission_cny",
    "opening_auction_policy",
    "price_limit_mode",
    "rights_issue_policy",
)
_STRICT_EVENT_CODES = (
    "event.financial_results.annual_report",
    "event.financial_results.earnings_flash_report",
    "event.financial_results.earnings_forecast_published",
    "event.financial_results.quarterly_report",
    "event.financial_results.semiannual_report",
)
_CASES = {
    "technical": f"东方财富 MACD 金叉买入，死叉卖出，回测 {_START} 至 {_END}",
    "event": f"东方财富 年度报告发布后买入，MACD 死叉卖出，回测 {_START} 至 {_END}",
}


class LiveE2EFailure(RuntimeError):
    pass


class ApiClient:
    def __init__(
        self,
        base_url: str,
        timeout: float,
        *,
        transport_attempts: int = 3,
        retry_delay_seconds: float = 0.25,
    ) -> None:
        if transport_attempts < 1:
            raise ValueError("transport_attempts must be positive")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds cannot be negative")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport_attempts = transport_attempts
        self._retry_delay_seconds = retry_delay_seconds
        self._invocation_id = uuid.uuid4().hex
        self._request_sequence = 0

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> Any:
        data = None if payload is None else _json_bytes(payload)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Request-ID": "strict-live-e2e",
        }
        if method == "POST":
            # A transport failure can happen after the server accepted the body.
            # Reuse one key within this logical request so a retry cannot create
            # another draft/run.  The invocation nonce prevents a later release
            # check from replaying a response produced by an older deployment.
            self._request_sequence += 1
            headers["Idempotency-Key"] = _idempotency_key(
                self._invocation_id,
                self._request_sequence,
                method,
                path,
                payload,
            )
        last_transport_error: BaseException | None = None
        for attempt in range(1, self._transport_attempts + 1):
            request = urllib.request.Request(
                f"{self._base_url}{path}",
                data=data,
                method=method,
                headers=headers,
            )
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    if response.status < 200 or response.status >= 300:
                        raise LiveE2EFailure(f"{method} {path} returned HTTP {response.status}")
                    if response.headers.get("X-Request-ID") != "strict-live-e2e":
                        raise LiveE2EFailure(f"{method} {path} lost X-Request-ID")
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:2_000]
                raise LiveE2EFailure(f"{method} {path} returned HTTP {exc.code}: {body}") from exc
            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                http.client.IncompleteRead,
                ssl.SSLError,
            ) as exc:
                last_transport_error = exc
                if attempt == self._transport_attempts:
                    break
                time.sleep(self._retry_delay_seconds * attempt)
        assert last_transport_error is not None
        reason = (
            last_transport_error.reason
            if isinstance(last_transport_error, urllib.error.URLError)
            else str(last_transport_error)
        )
        raise LiveE2EFailure(
            f"{method} {path} failed after {self._transport_attempts} transport attempts: {reason}"
        ) from last_transport_error


def run_live_e2e(
    client: ApiClient,
    *,
    wait_seconds: float,
) -> dict[str, object]:
    ready = _object(client.request("GET", "/api/v1/ready"), "ready")
    if ready.get("status") != "ready":
        raise LiveE2EFailure(f"strict readiness failed: {ready!r}")
    capabilities = _object(
        client.request("GET", "/api/v1/capabilities"),
        "capabilities",
    )
    if capabilities.get("backtest_execution_available") is not True:
        raise LiveE2EFailure("backtest execution is unavailable")
    if capabilities.get("event_backtest_available") is not True:
        raise LiveE2EFailure("event backtesting is unavailable")
    _validate_strict_event_capabilities(capabilities)
    version = _object(client.request("GET", "/api/v1/version"), "version")

    cases = {
        name: _run_case(client, name=name, utterance=utterance, wait_seconds=wait_seconds)
        for name, utterance in _CASES.items()
    }
    _validate_cross_case_identity(cases)
    return {
        "schemaVersion": "ashare-lab.strict-live-e2e.v1",
        "fixedInputs": {
            "instrument": "300059.SZ",
            "start": _START,
            "end": _END,
            "initialCashCny": _INITIAL_CASH,
        },
        "ready": ready,
        "capabilities": capabilities,
        "version": version,
        "cases": cases,
    }


def _run_case(
    client: ApiClient,
    *,
    name: str,
    utterance: str,
    wait_seconds: float,
) -> dict[str, object]:
    draft = _object(
        client.request(
            "POST",
            "/api/v1/strategy-drafts",
            {
                "utterance": utterance,
                "instrument_context": "300059.SZ",
                "as_of_date": _END,
            },
        ),
        f"{name} draft",
    )
    if draft.get("status") != "ready":
        raise LiveE2EFailure(f"{name} did not compile ready: {draft!r}")
    strategy = _object(draft.get("strategy"), f"{name} strategy")
    strategy_hash = _validated_draft_strategy_hash(draft, strategy=strategy, name=name)
    backtest = _object(strategy.get("backtest"), f"{name} backtest")
    if (
        backtest.get("start") != _START
        or backtest.get("end") != _END
        or backtest.get("initial_cash_cny") != _INITIAL_CASH
    ):
        raise LiveE2EFailure(f"{name} changed the fixed backtest inputs: {backtest!r}")
    entry = _object(strategy.get("entry"), f"{name} entry")
    expected_entry = "event_condition" if name == "event" else "indicator_condition"
    if entry.get("type") != expected_entry:
        raise LiveE2EFailure(f"{name} compiled the wrong entry type: {entry!r}")

    created = _object(
        client.request(
            "POST",
            "/api/v1/backtest-runs",
            {"strategy": strategy, "config": {"runRobustness": True}},
        ),
        f"{name} created run",
    )
    run_id = created.get("id")
    if not isinstance(run_id, str) or not run_id.startswith("run:"):
        raise LiveE2EFailure(f"{name} returned an invalid run ID")
    replay_result_hash = _validate_created_run(created, name=name)
    completed = _wait_for_run(client, run_id, wait_seconds)
    result_hash = _completed_result_hash(completed, name=f"{name} status")
    _validate_status_identity(created, completed, name=name)
    if replay_result_hash is not None and replay_result_hash != result_hash:
        raise LiveE2EFailure(
            f"{name} completed replay changed resultHash: {replay_result_hash!r} != {result_hash!r}"
        )
    # Every result-view endpoint revalidates the complete persisted bundle on the server.
    # The final status read below anchors those views to the same immutable result hash.
    summary = _object(
        client.request("GET", f"/api/v1/backtest-runs/{run_id}/summary"),
        f"{name} summary",
    )
    series = _list(
        client.request("GET", f"/api/v1/backtest-runs/{run_id}/series"),
        f"{name} series",
    )
    activities = _list(
        client.request("GET", f"/api/v1/backtest-runs/{run_id}/trades"),
        f"{name} activities",
    )
    if not series:
        raise LiveE2EFailure(f"{name} returned no series")
    if not activities:
        raise LiveE2EFailure(f"{name} returned no auditable activities")
    if summary.get("runId") != run_id:
        raise LiveE2EFailure(f"{name} summary belongs to another run: {summary.get('runId')!r}")
    _validate_run_evidence(summary, name=name, expected_strategy_hash=strategy_hash)
    if summary.get("initialCashCny") != _INITIAL_CASH:
        raise LiveE2EFailure(f"{name} did not run with 1,000,000 CNY")
    if name == "event" and not _contains_event_provenance(activities):
        raise LiveE2EFailure("event activities do not preserve second-level source provenance")
    verified = _object(
        client.request("GET", f"/api/v1/backtest-runs/{run_id}"),
        f"{name} verified status",
    )
    verified_hash = _completed_result_hash(verified, name=f"{name} verified status")
    _validate_status_identity(completed, verified, name=name)
    if verified_hash != result_hash:
        raise LiveE2EFailure(
            f"{name} resultHash changed while reading summary/series/trades: "
            f"{result_hash!r} != {verified_hash!r}"
        )
    return {
        "utterance": utterance,
        "draft": draft,
        "created": created,
        "completed": completed,
        "resultHash": result_hash,
        "summary": summary,
        "series": series,
        "seriesSha256": _sha256(series),
        "activities": activities,
        "activitiesSha256": _sha256(activities),
    }


def _wait_for_run(client: ApiClient, run_id: str, wait_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + wait_seconds
    last: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        last = _object(client.request("GET", f"/api/v1/backtest-runs/{run_id}"), "run status")
        if last.get("state") == "succeeded":
            _completed_result_hash(last, name="run status")
            return last
        if last.get("state") in {"failed", "cancelled"}:
            raise LiveE2EFailure(f"{run_id} ended as {last!r}")
        if last.get("resultAvailable") is not False or last.get("resultHash") is not None:
            raise LiveE2EFailure(f"{run_id} exposed a result before succeeding: {last!r}")
        time.sleep(0.25)
    raise LiveE2EFailure(f"{run_id} did not finish in {wait_seconds:g}s; last={last!r}")


def _validate_created_run(created: Mapping[str, object], *, name: str) -> str | None:
    replayed = created.get("replayed")
    if not isinstance(replayed, bool):
        raise LiveE2EFailure(f"{name} create response omitted the replay identity")
    result_available = created.get("resultAvailable")
    result_hash = created.get("resultHash")
    state = created.get("state")
    if result_available is True:
        if replayed is not True or state != "succeeded":
            raise LiveE2EFailure(
                f"{name} exposed a result outside a succeeded idempotent replay: {created!r}"
            )
        return _completed_result_hash(created, name=f"{name} completed replay")
    if result_available is not False or result_hash is not None:
        raise LiveE2EFailure(f"{name} returned inconsistent pre-result state: {created!r}")
    if state == "succeeded":
        raise LiveE2EFailure(f"{name} succeeded without an available result")
    return None


def _completed_result_hash(status: Mapping[str, object], *, name: str) -> str:
    if status.get("state") != "succeeded" or status.get("resultAvailable") is not True:
        raise LiveE2EFailure(f"{name} is not a completed result: {status!r}")
    result_hash = status.get("resultHash")
    if not isinstance(result_hash, str) or _HASH.fullmatch(result_hash) is None:
        raise LiveE2EFailure(f"{name} did not expose a valid resultHash")
    return result_hash


def _validate_status_identity(
    before: Mapping[str, object],
    after: Mapping[str, object],
    *,
    name: str,
) -> None:
    for label, status in (("before", before), ("after", after)):
        fingerprint = status.get("fingerprint")
        if not isinstance(fingerprint, str) or _HASH.fullmatch(fingerprint) is None:
            raise LiveE2EFailure(f"{name} {label} status has an invalid manifest fingerprint")
    for key in ("id", "fingerprint"):
        if before.get(key) != after.get(key):
            raise LiveE2EFailure(
                f"{name} changed run {key} while awaiting/reading results: "
                f"{before.get(key)!r} != {after.get(key)!r}"
            )


def _validated_draft_strategy_hash(
    draft: Mapping[str, object],
    *,
    strategy: Mapping[str, object],
    name: str,
) -> str:
    draft_strategy_hash = draft.get("strategy_hash")
    if not isinstance(draft_strategy_hash, str) or _HASH.fullmatch(draft_strategy_hash) is None:
        raise LiveE2EFailure(f"{name} draft has an invalid strategy_hash")
    computed_strategy_hash = canonical_hash(strategy)
    if draft_strategy_hash != computed_strategy_hash:
        raise LiveE2EFailure(
            f"{name} draft strategy_hash does not match its strategy: "
            f"{draft_strategy_hash!r} != {computed_strategy_hash!r}"
        )
    return draft_strategy_hash


def _validate_run_evidence(
    summary: Mapping[str, object],
    *,
    name: str,
    expected_strategy_hash: str,
) -> None:
    evidence = _object(summary.get("runEvidence"), f"{name} run evidence")
    for key in ("strategyHash", "catalogHash", "dataSnapshotChecksum"):
        value = evidence.get(key)
        if not isinstance(value, str) or _HASH.fullmatch(value) is None:
            raise LiveE2EFailure(f"{name} run evidence has invalid {key}: {value!r}")
    if evidence.get("strategyHash") != expected_strategy_hash:
        raise LiveE2EFailure(
            f"{name} result strategyHash does not match the compiled draft: "
            f"{evidence.get('strategyHash')!r} != {expected_strategy_hash!r}"
        )
    snapshot_id = evidence.get("dataSnapshotId")
    if not isinstance(snapshot_id, str) or _SLICE_ID.fullmatch(snapshot_id) is None:
        raise LiveE2EFailure(f"{name} does not identify an immutable pinned slice: {snapshot_id!r}")
    code_revision = evidence.get("codeRevision")
    if not isinstance(code_revision, str) or _CLEAN_SHA.fullmatch(code_revision) is None:
        raise LiveE2EFailure(f"{name} does not identify a clean Git revision")
    engine_version = evidence.get("engineVersion")
    if not isinstance(engine_version, str) or not engine_version.strip():
        raise LiveE2EFailure(f"{name} does not identify the backtest engine version")
    if evidence.get("dataSchemaVersion") != _PIN_SCHEMA:
        raise LiveE2EFailure(
            f"{name} did not use the pinned loader schema: {evidence.get('dataSchemaVersion')!r}"
        )
    if evidence.get("producerSnapshotSchemaVersion") != _COMPOSITE_SCHEMA:
        raise LiveE2EFailure(
            f"{name} did not use the strict composite v2 producer schema: "
            f"{evidence.get('producerSnapshotSchemaVersion')!r}"
        )
    producer_snapshot_id = evidence.get("producerSnapshotId")
    if (
        not isinstance(producer_snapshot_id, str)
        or _COMPOSITE_ID.fullmatch(producer_snapshot_id) is None
    ):
        raise LiveE2EFailure(
            f"{name} does not identify the immutable composite producer snapshot: "
            f"{producer_snapshot_id!r}"
        )
    assumptions = _object(evidence.get("executionAssumptions"), f"{name} assumptions")
    for key in _REQUIRED_EXECUTION_ASSUMPTIONS:
        value = assumptions.get(key)
        if not isinstance(value, str) or not value.strip():
            raise LiveE2EFailure(f"{name} run evidence is missing execution assumption {key}")


def _validate_cross_case_identity(cases: dict[str, dict[str, object]]) -> None:
    evidence = [
        _object(_object(case["summary"], "summary").get("runEvidence"), "run evidence")
        for case in cases.values()
    ]
    for key in (
        "dataSnapshotId",
        "dataSnapshotChecksum",
        "dataSchemaVersion",
        "producerSnapshotId",
        "producerSnapshotSchemaVersion",
        "codeRevision",
        "engineVersion",
        "catalogHash",
    ):
        values = {item.get(key) for item in evidence}
        if len(values) != 1 or None in values:
            raise LiveE2EFailure(f"technical/event runs do not share {key}: {values!r}")
    assumption_hashes = {
        _sha256(_object(item.get("executionAssumptions"), "execution assumptions"))
        for item in evidence
    }
    if len(assumption_hashes) != 1:
        raise LiveE2EFailure(
            "technical/event runs do not share fee, market-rule, and execution policies: "
            f"{assumption_hashes!r}"
        )


def _validate_strict_event_capabilities(capabilities: Mapping[str, object]) -> None:
    raw_events = capabilities.get("events")
    if not isinstance(raw_events, list):
        raise LiveE2EFailure("capabilities.events must be a list")
    by_code: dict[str, Mapping[object, object]] = {}
    for raw_event in cast(list[object], raw_events):
        if not isinstance(raw_event, Mapping):
            continue
        event = cast(Mapping[object, object], raw_event)
        event_code = event.get("event_code")
        if isinstance(event_code, str):
            by_code[event_code] = event
    for event_code in _STRICT_EVENT_CODES:
        event = by_code.get(event_code)
        if event is None:
            raise LiveE2EFailure(f"strict event capability is missing: {event_code}")
        if (
            event.get("backtest_available") is not True
            or event.get("availability_scope") != "pinned_snapshot"
        ):
            raise LiveE2EFailure(
                f"strict event capability is not pinned and runnable: {event_code}: {event!r}"
            )


def _contains_event_provenance(activities: list[Any]) -> bool:
    for raw_activity in activities:
        if not isinstance(raw_activity, Mapping):
            continue
        activity = cast(Mapping[object, object], raw_activity)
        raw_evidence = activity.get("evidence")
        if not isinstance(raw_evidence, list):
            continue
        for raw_item in cast(list[object], raw_evidence):
            if not isinstance(raw_item, Mapping):
                continue
            item = cast(Mapping[object, object], raw_item)
            if (
                item.get("sourceEventId")
                and item.get("provider")
                and item.get("rawResponseSha256")
                and isinstance(item.get("availableAt"), str)
                and item.get("timestampPrecision") == "second"
                and item.get("timeQuality") in {"exact", "vendor_observed"}
                and item.get("validationStatus") == "validated"
            ):
                return True
    return False


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LiveE2EFailure(f"{label} must be an object")
    return cast(dict[str, Any], value)


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise LiveE2EFailure(f"{label} must be a list")
    return cast(list[Any], value)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _idempotency_key(
    invocation_id: str,
    request_sequence: int,
    method: str,
    path: str,
    payload: dict[str, object] | None,
) -> str:
    digest = hashlib.sha256(
        _json_bytes(
            {
                "invocationId": invocation_id,
                "requestSequence": request_sequence,
                "method": method,
                "path": path,
                "payload": payload,
            }
        )
    ).hexdigest()
    return f"strict-live-e2e:{digest[:32]}"


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_json_bytes(value)).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--wait-seconds", type=float, default=180)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.timeout <= 0 or args.wait_seconds <= 0:
        parser.error("timeouts must be positive")
    try:
        evidence = run_live_e2e(
            ApiClient(args.base_url, args.timeout),
            wait_seconds=args.wait_seconds,
        )
    except LiveE2EFailure as exc:
        print(f"STRICT LIVE E2E FAILED: {exc}", file=sys.stderr)
        return 1
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_json_bytes(evidence) + b"\n")
    cases = cast(dict[str, dict[str, object]], evidence["cases"])
    print(
        json.dumps(
            {
                "status": "passed",
                "output": str(output),
                "snapshotId": _object(
                    _object(cases["technical"]["summary"], "summary").get("runEvidence"),
                    "run evidence",
                )["dataSnapshotId"],
                "technicalRunId": _object(cases["technical"]["completed"], "completed")["id"],
                "eventRunId": _object(cases["event"]["completed"], "completed")["id"],
                "technicalResultHash": cases["technical"]["resultHash"],
                "eventResultHash": cases["event"]["resultHash"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
