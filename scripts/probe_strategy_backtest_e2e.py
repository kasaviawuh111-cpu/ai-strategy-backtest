#!/usr/bin/env python3
"""Run one natural-language strategy through the local backtest API.

The probe talks only to a local HTTP service.  It never reads or records API
credentials.  When a local SQLite database is supplied, it also verifies that
provider indicator evidence was persisted with the run instead of silently
falling back to locally calculated indicators.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})


def _request_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            detail = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            detail = raw.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"local API returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"local API request failed: {type(exc).__name__}") from exc
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("local API returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("local API returned a non-object JSON root")
    return cast(dict[str, Any], decoded)


def _wait_for_run(
    base_url: str,
    run_id: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = _request_json(
            "GET",
            f"{base_url}/api/v1/backtest-runs/{run_id}",
            timeout_seconds=min(30.0, timeout_seconds),
        )
        if status.get("state") in _TERMINAL_STATES:
            return status
        if time.monotonic() >= deadline:
            raise RuntimeError(f"backtest did not finish within {timeout_seconds:g} seconds")
        time.sleep(poll_interval_seconds)


def _provider_evidence(database_path: Path, run_id: str) -> dict[str, Any]:
    resolved = database_path.expanduser().resolve()
    if not resolved.is_file():
        raise RuntimeError(f"SQLite database does not exist: {resolved}")
    connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT config_json, manifest_json FROM backtest_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise RuntimeError(f"run is missing from SQLite database: {run_id}")
    config = json.loads(str(row[0]))
    manifest = json.loads(str(row[1]))
    if not isinstance(config, dict) or not isinstance(manifest, dict):
        raise RuntimeError("persisted run JSON is invalid")
    payload = config.get("provider_indicator_series")
    manifest_payload = manifest.get("provider_indicator_series")
    if not isinstance(payload, dict):
        return {
            "persisted": False,
            "manifestMatched": manifest_payload is None,
            "providers": [],
            "seriesCount": 0,
            "pointCount": 0,
            "responseHashes": [],
        }
    identity = payload.get("identity_basis")
    if not isinstance(identity, dict):
        raise RuntimeError("provider indicator identity basis is missing")
    series_items: list[dict[str, Any]] = []
    for side in ("entry", "exit"):
        raw_side = identity.get(side, [])
        if not isinstance(raw_side, list):
            raise RuntimeError("provider indicator side is not a list")
        for item in raw_side:
            if not isinstance(item, dict) or not isinstance(item.get("series"), dict):
                raise RuntimeError("provider indicator series item is invalid")
            series_items.append(cast(dict[str, Any], item["series"]))
    providers = sorted(
        {
            str(item.get("provider"))
            for item in series_items
            if isinstance(item.get("provider"), str)
        }
    )
    response_hashes = sorted(
        {
            str(item.get("response_sha256"))
            for item in series_items
            if isinstance(item.get("response_sha256"), str)
        }
    )
    point_count = sum(
        len(points) if isinstance((points := item.get("points")), list) else 0
        for item in series_items
    )
    return {
        "persisted": True,
        "manifestMatched": manifest_payload == payload,
        "schemaVersion": payload.get("schema_version"),
        "snapshotId": payload.get("snapshot_id"),
        "providers": providers,
        "seriesCount": len(series_items),
        "pointCount": point_count,
        "responseHashes": response_hashes,
    }


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    resolved = path.expanduser().resolve()
    parent_existed = resolved.parent.exists()
    resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        resolved.parent.chmod(0o700)
    resolved.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    resolved.chmod(0o600)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--utterance", required=True)
    parser.add_argument("--as-of-date", type=date.fromisoformat, required=True)
    parser.add_argument("--local-api-base", default="http://127.0.0.1:8017")
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0)
    parser.add_argument("--sqlite-db", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.poll_interval_seconds <= 0:
        parser.error("--poll-interval-seconds must be positive")
    if not 0 <= args.slippage_bps <= 1_000:
        parser.error("--slippage-bps must be between 0 and 1000")
    return args


def main() -> int:
    args = _parse_args()
    base_url = args.local_api_base.rstrip("/")
    started = time.monotonic()
    draft = _request_json(
        "POST",
        f"{base_url}/api/v1/strategy-drafts",
        payload={
            "utterance": args.utterance,
            "as_of_date": args.as_of_date.isoformat(),
        },
    )
    if draft.get("status") != "ready" or not isinstance(draft.get("strategy"), dict):
        result = {
            "status": "compile_not_ready",
            "diagnosticCode": draft.get("diagnostic_code"),
            "clarification": draft.get("clarification"),
            "elapsedSeconds": round(time.monotonic() - started, 3),
        }
        if args.output is not None:
            _write_private(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    created = _request_json(
        "POST",
        f"{base_url}/api/v1/backtest-runs",
        payload={
            "strategy": draft["strategy"],
            "config": {"slippageBps": args.slippage_bps},
        },
        timeout_seconds=min(args.timeout_seconds, 300.0),
    )
    run_id = created.get("id")
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError("backtest submission returned no run id")
    final = _wait_for_run(
        base_url,
        run_id,
        timeout_seconds=args.timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
    )
    result: dict[str, Any] = {
        "generatedAt": datetime.now(UTC).isoformat(),
        "status": final.get("state"),
        "runId": run_id,
        "replayed": created.get("replayed"),
        "strategyHash": draft.get("strategy_hash"),
        "instrument": cast(dict[str, Any], draft["strategy"]).get("instrument"),
        "backtest": cast(dict[str, Any], draft["strategy"]).get("backtest"),
        "slippageBps": args.slippage_bps,
        "elapsedSeconds": round(time.monotonic() - started, 3),
        "error": final.get("error"),
    }
    if final.get("state") == "succeeded":
        result["summary"] = _request_json(
            "GET",
            f"{base_url}/api/v1/backtest-runs/{run_id}/summary",
        )
    if args.sqlite_db is not None:
        result["providerIndicatorEvidence"] = _provider_evidence(args.sqlite_db, run_id)
    if args.output is not None:
        _write_private(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if final.get("state") == "succeeded" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"E2E probe failed: {exc}")
        raise SystemExit(2) from exc
