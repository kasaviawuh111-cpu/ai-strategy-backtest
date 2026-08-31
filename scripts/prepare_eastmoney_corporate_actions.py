#!/usr/bin/env python3
"""Acquire free Eastmoney corporate-action evidence for one A-share interval."""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ashare_lab.adapters.market_data.eastmoney_corporate_actions import (
    EastmoneyCorporateActionError,
    EastmoneyCorporateActionReferenceAdapter,
    to_choice_snapshot_payload,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def main() -> int:
    args = _parse_args()
    try:
        with EastmoneyCorporateActionReferenceAdapter() as adapter:
            result = adapter.prepare(
                symbol=args.symbol,
                start=args.start,
                end=args.end,
                captured_at=datetime.now(SHANGHAI).replace(microsecond=0),
            )
        payload = to_choice_snapshot_payload(result)
        _write_json_atomic(args.output, payload)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "path": str(args.output.expanduser().resolve()),
                    "instrumentId": result.instrument_id.value,
                    "corporateActionRows": len(result.corporate_actions),
                    "coverageStatus": result.coverage["status"],
                    "coverageScope": result.coverage["coverageScope"],
                    "strictEligibleUnderCurrentChoiceValidator": result.coverage[
                        "strictEligibleUnderCurrentChoiceValidator"
                    ],
                    "strictBlockers": result.coverage["strictBlockers"],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (EastmoneyCorporateActionError, OSError, TypeError, ValueError) as exc:
        print(f"Eastmoney corporate-action preparation failed: {exc}")
        return 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not exceed --end")
    return args


def _write_json_atomic(path: Path, payload: Any) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        stream.write(encoded)
        stream.write("\n")
        stream.flush()
    temporary.replace(destination)


if __name__ == "__main__":
    raise SystemExit(main())
