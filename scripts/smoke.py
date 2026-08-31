#!/usr/bin/env python3
"""Bounded HTTP smoke test for a deployed A-share Lab API."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from typing import Any, cast


class SmokeFailure(RuntimeError):
    pass


class ApiClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Request-ID": "deployment-smoke",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                if response.status < 200 or response.status >= 300:
                    raise SmokeFailure(f"{method} {path} returned HTTP {response.status}")
                request_id = response.headers.get("X-Request-ID")
                if request_id != "deployment-smoke":
                    raise SmokeFailure(f"{method} {path} did not preserve X-Request-ID")
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:1_000]
            raise SmokeFailure(f"{method} {path} returned HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise SmokeFailure(f"{method} {path} failed: {exc.reason}") from exc


def wait_until_available(client: ApiClient, wait_seconds: float, *, require_ready: bool) -> None:
    path = "/api/v1/ready" if require_ready else "/api/v1/health"
    expected_status = "ready" if require_ready else "ok"
    deadline = time.monotonic() + wait_seconds
    last_error: Exception | None = None
    while time.monotonic() <= deadline:
        try:
            payload = _object(client.request("GET", path), "availability")
            if payload.get("status") == expected_status:
                return
            last_error = SmokeFailure(f"unexpected {path} body: {payload!r}")
        except SmokeFailure as exc:
            last_error = exc
        time.sleep(1)
    raise SmokeFailure(f"API did not become available at {path}: {last_error}")


def run_smoke(
    client: ApiClient,
    *,
    require_backtest: bool,
    strategy_mode: str,
    as_of_date: date,
    backtest_wait_seconds: float,
) -> dict[str, Any]:
    version = _object(client.request("GET", "/api/v1/version"), "version")
    capabilities = _object(
        client.request("GET", "/api/v1/capabilities"),
        "capabilities",
    )
    openapi = _object(client.request("GET", "/api/v1/openapi.json"), "OpenAPI")

    required_paths = {
        "/api/v1/strategy-drafts",
        "/api/v1/backtest-runs",
        "/api/v1/backtest-runs/{run_id}",
        "/api/v1/ready",
    }
    paths = _object(openapi.get("paths"), "OpenAPI paths")
    missing = sorted(required_paths - set(paths))
    if missing:
        raise SmokeFailure(f"OpenAPI is missing paths: {', '.join(missing)}")

    available = capabilities.get("backtest_execution_available")
    if not isinstance(available, bool):
        raise SmokeFailure("capabilities has no boolean backtest_execution_available")
    if require_backtest and not available:
        raise SmokeFailure("backtest execution is required but unavailable")

    draft = _object(
        client.request(
            "POST",
            "/api/v1/strategy-drafts",
            {
                "utterance": (
                    "年报发布后买入，MACD死叉卖出"
                    if strategy_mode == "event"
                    else "MACD金叉买入，死叉卖出"
                ),
                "instrument_context": "300059.SZ",
                "as_of_date": as_of_date.isoformat(),
            },
        ),
        "strategy draft",
    )
    if draft.get("status") != "ready":
        raise SmokeFailure(f"strategy compiler did not return ready: {draft!r}")
    strategy_hash = draft.get("strategy_hash")
    if not isinstance(strategy_hash, str) or not strategy_hash.startswith("sha256:"):
        raise SmokeFailure("compiled strategy has no canonical sha256 hash")

    service_version = version.get("service_version")
    catalog_hash = version.get("catalog_snapshot_hash")
    if not isinstance(service_version, str) or not service_version:
        raise SmokeFailure("version endpoint has no service_version")
    if not isinstance(catalog_hash, str) or not catalog_hash.startswith("sha256:"):
        raise SmokeFailure("version endpoint has no catalog snapshot hash")
    result: dict[str, Any] = {
        "status": "ok",
        "serviceVersion": service_version,
        "catalogHash": catalog_hash,
        "backtestExecutionAvailable": available,
        "strategyHash": strategy_hash,
        "strategyMode": strategy_mode,
    }
    if require_backtest:
        strategy = _object(draft.get("strategy"), "compiled strategy")
        created = _object(
            client.request(
                "POST",
                "/api/v1/backtest-runs",
                {"strategy": strategy, "config": {"runRobustness": False}},
            ),
            "created backtest",
        )
        run_id = created.get("id")
        if not isinstance(run_id, str) or not run_id:
            raise SmokeFailure("created backtest has no run id")
        completed = wait_for_backtest(client, run_id, backtest_wait_seconds)
        summary = _object(
            client.request("GET", f"/api/v1/backtest-runs/{run_id}/summary"),
            "backtest summary",
        )
        series = client.request("GET", f"/api/v1/backtest-runs/{run_id}/series")
        trades = client.request("GET", f"/api/v1/backtest-runs/{run_id}/trades")
        if summary.get("runId") != run_id:
            raise SmokeFailure("backtest summary belongs to a different run")
        if not isinstance(series, list) or not series:
            raise SmokeFailure("backtest returned no equity series")
        if not isinstance(trades, list) or not trades:
            raise SmokeFailure("backtest returned no auditable activities")
        if strategy_mode == "event" and strategy.get("entry", {}).get("type") != "event_condition":
            raise SmokeFailure("event smoke did not compile an event entry condition")
        result.update(
            {
                "backtestRunId": run_id,
                "backtestState": completed.get("state"),
                "equityPoints": len(series),
                "activities": len(trades),
                "tradeCount": summary.get("tradeCount"),
                "totalReturn": summary.get("totalReturn"),
            }
        )
    return result


def wait_for_backtest(client: ApiClient, run_id: str, wait_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + wait_seconds
    last: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        last = _object(
            client.request("GET", f"/api/v1/backtest-runs/{run_id}"),
            "backtest status",
        )
        state = last.get("state")
        if state == "succeeded":
            return last
        if state in {"failed", "cancelled"}:
            raise SmokeFailure(
                f"backtest {run_id} ended in {state}: {last.get('error') or 'unknown error'}"
            )
        time.sleep(0.25)
    raise SmokeFailure(f"backtest {run_id} did not finish within {wait_seconds:g} seconds")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SmokeFailure(f"{label} response must be an object")
    return cast(dict[str, Any], value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--wait-seconds", type=float, default=30)
    parser.add_argument("--backtest-wait-seconds", type=float, default=120)
    parser.add_argument("--require-backtest", action="store_true")
    parser.add_argument("--strategy", choices=("technical", "event"), default="technical")
    parser.add_argument("--as-of-date", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    if args.timeout <= 0 or args.wait_seconds < 0 or args.backtest_wait_seconds <= 0:
        parser.error("timeouts must be positive")

    client = ApiClient(args.base_url, args.timeout)
    try:
        wait_until_available(client, args.wait_seconds, require_ready=args.require_backtest)
        result = run_smoke(
            client,
            require_backtest=args.require_backtest,
            strategy_mode=args.strategy,
            as_of_date=args.as_of_date,
            backtest_wait_seconds=args.backtest_wait_seconds,
        )
    except SmokeFailure as exc:
        print(f"SMOKE FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
