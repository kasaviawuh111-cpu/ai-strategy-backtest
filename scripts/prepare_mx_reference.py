#!/usr/bin/env python3
"""Acquire exact Eastmoney MX identity and historical A-share session facts."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ashare_lab.adapters.market_data.mx_saas import (
    MxSaasMarketDataClient,
    MxSaasProviderAuthError,
    MxSaasProviderDataError,
    MxSaasProviderUnavailableError,
)
from ashare_lab.adapters.market_data.mx_session_reference import (
    MxSessionReferenceAdapter,
    MxSessionReferenceError,
)
from ashare_lab.settings import AppSettings


def main() -> int:
    args = _parse_args()
    try:
        key = AppSettings(_env_file=None).resolved_mx_saas_api_key()
        if key is None:
            raise OSError("MX credential is unavailable")
        client = MxSaasMarketDataClient(
            api_key=key,
            timeout_seconds=args.timeout_seconds,
        )
        payload = asyncio.run(
            MxSessionReferenceAdapter(client).prepare(
                symbol=args.symbol,
                start=args.start,
                end=args.end,
                captured_at=datetime.now(UTC).replace(microsecond=0),
            )
        )
        _write_json_atomic(args.output, payload)
        sessions = payload["historicalSessions"]
        assert isinstance(sessions, dict)
        coverage = sessions["coverage"]
        assert isinstance(coverage, dict)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "path": str(args.output.expanduser().resolve()),
                    "instrument": payload["instrument"],
                    "historicalSessionRows": coverage["rowCount"],
                    "returnedStart": coverage["returnedStart"],
                    "returnedEnd": coverage["returnedEnd"],
                    "provider": coverage["provider"],
                    "aggregateAuditSha256": coverage["aggregateAuditSha256"],
                    "canonicalRowsSha256": coverage["canonicalRowsSha256"],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (
        MxSaasProviderUnavailableError,
        MxSaasProviderAuthError,
        MxSaasProviderDataError,
        MxSessionReferenceError,
        OSError,
        TimeoutError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"MX session reference failed: {type(exc).__name__}: {exc}")
        return 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not exceed --end")
    if not 1 <= args.timeout_seconds <= 120:
        parser.error("--timeout-seconds must be between 1 and 120")
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
