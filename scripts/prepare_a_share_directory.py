#!/usr/bin/env python3
"""Prepare the complete Eastmoney A-share identity directory, never market prices.

Install the optional instrument-directory extra to regenerate the pinyin index.
Use --input for a previously captured full response, or --refresh for one live
query through the application's existing MX transport. No model supplies names.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ashare_lab.adapters.market_data.a_share_directory import load
from ashare_lab.adapters.market_data.mx_saas import MxSaasMarketDataClient
from ashare_lab.domain.market_data.instruments import normalize_a_share_instrument
from ashare_lab.settings import AppSettings

QUERY = "全部A股，按股票代码从小到大排列，列出所有股票名称和股票代码，不限制条数。"
SOURCE_URL = "https://ai-saas.eastmoney.com/proxy/b/mcp/tool/selectSecurity"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "ashare_lab/resources/a_share_directory.json"


def build_payload(collected: Mapping[str, Any]) -> dict[str, Any]:
    """Project only identities after matching the provider's full-universe counts."""
    from pypinyin import Style, lazy_pinyin

    if collected.get("query") != QUERY:
        raise ValueError("directory input must use the exact unrestricted A-share query")
    rows = collected.get("rows")
    metadata = collected.get("provider_metadata", {})
    complete = metadata.get("allResults", {})
    total_condition = complete.get("totalCondition", {})
    total = total_condition.get("stockCount")
    if (metadata.get("selectType") != "A_STOCK" or complete.get("market") != "HSJ"
            or type(total) is not int or not 4_000 <= total <= 15_000
            or not isinstance(rows, (list, tuple)) or len(rows) != total):
        raise ValueError("provider full-market scope and actual row count do not agree")
    counts = [condition.get("stockCount")
              for part in (metadata, complete)
              for condition in part.get("responseConditionList", [])]
    if not counts or any(type(count) is not int or count != total for count in counts):
        raise ValueError("provider conditions narrowed or truncated the A-share universe")
    raw = collected.get("raw_provider_response")
    if raw is not None:
        result = raw.get("data", raw).get("allResults", {}).get("result", {})
        if (result.get("total") != total or result.get("totalRecordCount") != total
                or len(result.get("dataList", [])) != total):
            raise ValueError("raw response count does not match the directory")

    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        code, name = row.get("代码"), row.get("名称")
        if (not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code)
                or not isinstance(name, str) or not name.strip()):
            raise ValueError("a provider identity is incomplete")
        symbol = normalize_a_share_instrument(code).value
        if symbol in seen:
            raise ValueError("duplicate identity in full directory")
        seen.add(symbol)
        name = name.strip()
        pinyin = re.sub(r"[^a-z0-9]", "", "".join(lazy_pinyin(name)).lower())
        initials = re.sub(r"[^a-z0-9]", "", "".join(
            lazy_pinyin(name, style=Style.FIRST_LETTER),
        ).lower())
        items.append({"symbol": symbol, "name": name, "exchange": symbol.split(".")[1],
                      "pinyin": pinyin, "initials": initials})
    items.sort(key=lambda item: item["symbol"])
    return {
        "version": 1, "source": "eastmoney_instrument_directory", "source_url": SOURCE_URL,
        "retrieved_at": collected["collected_at_utc"], "reported_total": total,
        "coverage": "provider_current_all_a_shares_shenzhen_shanghai_beijing",
        "source_evidence": {
            "query": QUERY, "response_sha256": collected.get("response_sha256"),
            "condition": total_condition.get("describe"),
            "market_counts": dict(Counter(item["exchange"] for item in items)),
            "pinyin": "derived locally with pypinyin 0.55.0; not provider identity evidence",
        },
        "items": items,
    }


async def collect() -> dict[str, Any]:
    settings = AppSettings()
    key = settings.resolved_mx_saas_api_key()
    if not key:
        raise ValueError("Eastmoney credentials are not configured; directory unchanged")
    result = await MxSaasMarketDataClient(
        api_key=key, timeout_seconds=60, max_attempts=1,
    ).screen(query=QUERY, asset_type="A股")
    return {"query": QUERY, "rows": list(result.rows),
            "provider_metadata": result.provider_metadata,
            "collected_at_utc": result.provenance.retrieved_at.isoformat(),
            "response_sha256": result.provenance.response_sha256}


def write_directory(payload: Mapping[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, prefix=".directory-", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        load(temporary)  # Same production validation before publishing any replacement.
        temporary.chmod(0o644)  # Public identities must remain readable by container app users.
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path)
    source.add_argument("--refresh", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--csv", type=Path, help="Optional plain names/codes export")
    args = parser.parse_args()
    collected = (json.loads(args.input.read_text(encoding="utf-8")) if args.input
                 else asyncio.run(collect()))
    payload = build_payload(collected)
    write_directory(payload, args.output)
    directory = load(args.output)
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("股票代码", "股票名称", "交易所"))
            writer.writerows((item.symbol, item.name, item.exchange)
                             for item in directory.items)
    print(json.dumps({"path": str(args.output.resolve()), "count": len(directory.items),
                      "markets": dict(Counter(item.exchange for item in directory.items)),
                      "retrieved_at": directory.retrieved_at.isoformat()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
