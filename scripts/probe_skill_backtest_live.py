#!/usr/bin/env python3
"""Minimal real Eastmoney-history to backtest-result HTTP smoke probe.

This intentionally starts from a fixed, already-canonical MA20 strategy.  It
does not test natural-language/model generation and must not be cited as model
acceptance evidence.  Output is limited to one JSON summary or one JSON error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
    timeout: float,
) -> dict[str, Any] | list[dict[str, Any]]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        decoded: object = json.load(response)
    if isinstance(decoded, dict):
        return cast(dict[str, Any], decoded)
    if isinstance(decoded, list):
        items = cast(list[object], decoded)
        if all(isinstance(item, dict) for item in items):
            return cast(list[dict[str, Any]], items)
    raise RuntimeError("local API returned an unexpected JSON shape")


_SYMBOL = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")


def _strategy_request(symbol: str = "300059.SZ") -> dict[str, object]:
    condition = {
        "type": "indicator_condition",
        "indicator_id": "technical.ma",
        "definition_version": "1.0.0",
        "params": {"period": 20, "price_field": "close"},
        "timeframe": "1d",
        "evaluation_mode": "bar_close_confirmed",
    }
    return {
        "strategy": {
            "schema_version": "strategy.v1",
            "catalog": {
                "catalog_id": "cn_a.signals",
                "release_version": "2026.09.01",
            },
            "instrument": {
                "market": "CN_A",
                "symbol": symbol,
                "position_mode": "long_only",
            },
            "entry": {**condition, "trigger": "price_crosses_above"},
            "exit": {
                "op": "first_of",
                "children": [{**condition, "trigger": "price_crosses_below"}],
            },
            "execution": {
                "data_capability": "daily_ohlcv",
                "execution_resolution": "1d",
                "evaluation_frequency": "1d_close",
                "position_policy": "single_position_no_pyramiding",
                "t_plus_one": True,
            },
            "backtest": {
                "start": "2025-09-04",
                "end": "2026-09-04",
                "initial_cash_cny": 100000,
            },
        },
        "config": {
            "capacityMode": "point_in_time_volume",
            "participationRate": "0.05",
            "slippageBps": "5",
            "allocationRatio": "1",
            "limitHandling": "wait_for_unlock",
            "commissionRate": "0.0003",
            "minimumCommissionCny": "5",
            "retryUnfilledExits": True,
            "maxExitAttempts": 20,
            "edgeEntryValiditySessions": 3,
            "eventEntryValiditySessions": 1,
            "stateEntryValiditySessions": 1,
            "warmupCalendarDays": 180,
            "settlementExtensionDays": 14,
            "runRobustness": True,
        },
    }


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} is missing")
    return cast(dict[str, Any], value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8011")
    parser.add_argument("--symbol", default="300059.SZ")
    parser.add_argument("--request-timeout", type=float, default=30)
    parser.add_argument("--run-timeout", type=float, default=240)
    args = parser.parse_args()
    symbol = args.symbol.strip().upper()
    if _SYMBOL.fullmatch(symbol) is None:
        parser.error("--symbol must be a canonical A-share code such as 300059.SZ")

    try:
        created = _mapping(
            _request(
                args.base_url,
                "/api/v1/backtest-runs",
                method="POST",
                body=_strategy_request(symbol),
                timeout=args.request_timeout,
            ),
            "created run",
        )
        run_id = str(created["id"])
        deadline = time.monotonic() + args.run_timeout
        status = created
        while str(status.get("state")) not in {"succeeded", "failed", "cancelled"}:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"backtest did not finish in {args.run_timeout:g}s")
            time.sleep(1)
            status = _mapping(
                _request(
                    args.base_url,
                    f"/api/v1/backtest-runs/{run_id}",
                    timeout=args.request_timeout,
                ),
                "run status",
            )
        if status.get("state") != "succeeded":
            raise RuntimeError(
                f"run ended as {status.get('state')}: {status.get('error') or 'unknown error'}"
            )
        summary = _mapping(
            _request(
                args.base_url,
                f"/api/v1/backtest-runs/{run_id}/summary",
                timeout=args.request_timeout,
            ),
            "run summary",
        )
        series = _request(
            args.base_url,
            f"/api/v1/backtest-runs/{run_id}/series",
            timeout=args.request_timeout,
        )
        activities = _request(
            args.base_url,
            f"/api/v1/backtest-runs/{run_id}/trades",
            timeout=args.request_timeout,
        )
        if not isinstance(series, list) or not isinstance(activities, list):
            raise RuntimeError("series or activities endpoint returned a non-list")
        provenance = _mapping(summary.get("dataProvenance"), "dataProvenance")
        data_range = _mapping(summary.get("dataRange"), "dataRange")
        if provenance.get("provider") != "eastmoney_mx_finance_data":
            raise RuntimeError("result does not prove the Eastmoney MX provider")
        if provenance.get("priceBasis") != "provider_back_adjusted":
            raise RuntimeError("result does not declare provider-adjusted returns")
        if int(provenance.get("historyRows", 0)) <= 0:
            raise RuntimeError("historyRows is empty")
        if int(provenance.get("indicatorPoints", 0)) <= 0:
            raise RuntimeError("indicatorPoints is empty")
        if int(data_range.get("sessions", -1)) != len(series) - 1:
            raise RuntimeError("dataRange.sessions is inconsistent with the equity curve")
        if not status.get("resultHash"):
            raise RuntimeError("completed run has no result hash")
        fills = [
            item for item in activities if item.get("kind") in {"fill", "partial_fill"}
        ]
        if not fills:
            raise RuntimeError("fixed MA20 smoke produced no simulated fills")
        if any(item.get("quantity") is not None for item in fills):
            raise RuntimeError("skill simulation invented exchange-share quantities")
        if any(float(item.get("notionalCny") or 0) <= 0 for item in fills):
            raise RuntimeError("simulated fill is missing positive notionalCny")
        if "不是逐笔分红到账的实盘账户" not in str(summary.get("interpretation", "")):
            raise RuntimeError("result does not disclose the research-simulation limitation")
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "scope": "real_mx_data_to_skill_engine_to_result_adapter_only",
                    "notModelAcceptance": True,
                    "instrumentId": symbol,
                    "runId": run_id,
                    "resultHash": status["resultHash"],
                    "provider": provenance["provider"],
                    "priceBasis": provenance["priceBasis"],
                    "historyRows": provenance["historyRows"],
                    "indicatorPoints": provenance["indicatorPoints"],
                    "seriesPoints": len(series),
                    "activities": len(activities),
                    "tradeCount": summary.get("tradeCount"),
                    "totalReturn": summary.get("totalReturn"),
                    "benchmarkReturn": summary.get("benchmarkReturn"),
                    "finalEquityCny": summary.get("finalEquityCny"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except HTTPError as exc:
        try:
            detail = exc.read(2_000).decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - error reporting must not mask the HTTP error
            detail = "response body unavailable"
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "errorType": type(exc).__name__,
                    "error": f"HTTP {exc.code}: {detail}"[:500],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    except (URLError, KeyError, RuntimeError, TimeoutError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "errorType": type(exc).__name__,
                    "error": str(exc)[:500],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
