#!/usr/bin/env python3
"""Fetch recent Eastmoney daily fund-flow data into a research-only JSON snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ashare_lab.adapters.fund_flow import (
    EastmoneyFundFlowResearchSource,
    build_fund_flow_research_snapshot,
)
from ashare_lab.domain.shared import InstrumentId


def main() -> int:
    args = _parse_args()
    try:
        with EastmoneyFundFlowResearchSource(timeout=args.timeout) as source:
            collection = source.fetch(
                instrument_id=InstrumentId(args.symbol.upper()),
                limit=args.limit,
            )
        result = build_fund_flow_research_snapshot(
            collection=collection,
            output_root=args.output_root,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"Fund-flow research snapshot failed: {exc}")
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "mode": "research_only",
                "snapshotId": result.snapshot_id,
                "path": str(result.path),
                "rows": len(collection.rows),
                "runtimeNetworkFetchAllowed": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="300059.SZ")
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("var/snapshots/fund_flow_research"),
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= 120:
        parser.error("--limit must be between 1 and 120")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


if __name__ == "__main__":
    raise SystemExit(main())
