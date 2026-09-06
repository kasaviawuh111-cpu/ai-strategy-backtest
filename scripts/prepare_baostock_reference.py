#!/usr/bin/env python3
"""Acquire SH/SZ metadata, corporate actions, and historical session evidence."""

from __future__ import annotations

import argparse
import json
import socket
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ashare_lab.adapters.market_data.baostock_reference import (
    BaoStockReferenceAdapter,
    BaoStockReferenceError,
    to_choice_snapshot_payload,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def main() -> int:
    args = _parse_args()
    try:
        import baostock as bs
    except ImportError:
        print("BaoStock is not installed; install the project demo extra.")
        return 2

    previous_socket_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(args.socket_timeout_seconds)
    try:
        login = bs.login()
        if str(login.error_code) != "0":
            print(f"BaoStock login failed: {login.error_code} {login.error_msg}")
            return 1
        result = BaoStockReferenceAdapter(bs).prepare(
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
                    "instrument": payload["instrument"],
                    "corporateActionRows": len(result.corporate_actions),
                    "historicalSessionRows": len(result.historical_sessions),
                    "coverage": payload["coverage"],
                    "historicalSessionCoverage": payload["historicalSessions"]["coverage"],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (BaoStockReferenceError, OSError, TypeError, ValueError) as exc:
        print(f"BaoStock reference preparation failed: {exc}")
        return 1
    finally:
        try:
            if "login" in locals() and str(login.error_code) == "0":
                bs.logout()
        finally:
            socket.setdefaulttimeout(previous_socket_timeout)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--socket-timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not exceed --end")
    if not 1 <= args.socket_timeout_seconds <= 120:
        parser.error("--socket-timeout-seconds must be between 1 and 120")
    return args


def _write_json_atomic(path: Path, payload: Any) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
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
