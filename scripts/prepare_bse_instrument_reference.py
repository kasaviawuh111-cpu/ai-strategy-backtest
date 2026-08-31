#!/usr/bin/env python3
"""Acquire and atomically persist one official current BSE instrument reference."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from ashare_lab.adapters.market_data.bse_reference import (
    BseInstrumentReferenceError,
    BseInstrumentReferenceSource,
    to_instrument_reference_payload,
)


def main() -> int:
    args = _parse_args()
    try:
        with BseInstrumentReferenceSource() as source:
            result = source.fetch(args.symbol)
        payload = to_instrument_reference_payload(result)
        _write_json_atomic(args.output, payload)
    except (BseInstrumentReferenceError, OSError, TypeError, ValueError) as exc:
        print(f"BSE instrument reference preparation failed: {exc}")
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "path": str(args.output.expanduser().resolve()),
                "instrument": payload["instrument"],
                "coverage": payload["coverage"],
                "queryAudit": payload["queryAudit"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


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
