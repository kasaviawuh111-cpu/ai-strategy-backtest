#!/usr/bin/env python3
"""Run the fixed technical and event acceptance cases against a live strict API."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

_CLEAN_SHA = re.compile(r"^[0-9a-f]{40}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_START = "2021-08-06"
_END = "2026-08-06"
_INITIAL_CASH = 1_000_000
_PIN_SCHEMA = "local-parquet.market-data.v3"
_COMPOSITE_SCHEMA = "ashare-lab.composite-research-snapshot.v2"
_COMPOSITE_ID = re.compile(r"^composite:[0-9a-f]{64}$")
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
    def __init__(self, base_url: str, timeout: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> Any:
        data = None if payload is None else _json_bytes(payload)
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Request-ID": "strict-live-e2e",
            },
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
        except urllib.error.URLError as exc:
            raise LiveE2EFailure(f"{method} {path} failed: {exc.reason}") from exc


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
    if created.get("resultAvailable") is not False or created.get("resultHash") is not None:
        raise LiveE2EFailure(f"{name} exposed a result before the run succeeded")
    completed = _wait_for_run(client, run_id, wait_seconds)
    result_hash = completed.get("resultHash")
    if not isinstance(result_hash, str) or _HASH.fullmatch(result_hash) is None:
        raise LiveE2EFailure(f"{name} status did not expose a valid resultHash")
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
    evidence = _object(summary.get("runEvidence"), f"{name} run evidence")
    code_revision = evidence.get("codeRevision")
    if not isinstance(code_revision, str) or _CLEAN_SHA.fullmatch(code_revision) is None:
        raise LiveE2EFailure(f"{name} does not identify a clean Git revision")
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
    if summary.get("initialCashCny") != _INITIAL_CASH:
        raise LiveE2EFailure(f"{name} did not run with 1,000,000 CNY")
    if name == "event" and not _contains_event_provenance(activities):
        raise LiveE2EFailure("event activities do not preserve second-level source provenance")
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
            return last
        if last.get("state") in {"failed", "cancelled"}:
            raise LiveE2EFailure(f"{run_id} ended as {last!r}")
        if last.get("resultAvailable") is not False or last.get("resultHash") is not None:
            raise LiveE2EFailure(f"{run_id} exposed a result before succeeding: {last!r}")
        time.sleep(0.25)
    raise LiveE2EFailure(f"{run_id} did not finish in {wait_seconds:g}s; last={last!r}")


def _validate_cross_case_identity(cases: dict[str, dict[str, object]]) -> None:
    evidence = [
        _object(_object(case["summary"], "summary").get("runEvidence"), "run evidence")
        for case in cases.values()
    ]
    for key in (
        "dataSnapshotId",
        "dataSnapshotChecksum",
        "producerSnapshotId",
        "codeRevision",
        "engineVersion",
    ):
        values = {item.get(key) for item in evidence}
        if len(values) != 1 or None in values:
            raise LiveE2EFailure(f"technical/event runs do not share {key}: {values!r}")


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
