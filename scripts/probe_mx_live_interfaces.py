#!/usr/bin/env python3
"""Probe the two Eastmoney MX data interfaces outside Codex network sandbox.

The script is intentionally dependency-free so it can be run with macOS
``/usr/bin/python3`` while the VPN is off.  It never prints or stores the API
key.  Raw provider replies and a small diagnostic summary are written under
``/private/tmp/ashare-mx-live-probe`` for inspection after network access is
restored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_PROVIDER_BASE = "https://ai-saas.eastmoney.com"
DEFAULT_KEY_FILE = Path.home() / ".mx-skills" / "em_api_key"
DEFAULT_OUTPUT_ROOT = Path("/private/tmp/ashare-mx-live-probe")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECURITY_CODE_RE = re.compile(r"^\d{6}(?:\.(?:SH|SZ|BJ))?$")


def _read_key(path: Path) -> str:
    key = os.environ.get("EM_API_KEY", "").strip()
    if not key and path.exists():
        key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError(
            "No API key found. Set EM_API_KEY or save it at ~/.mx-skills/em_api_key."
        )
    return key


def _request_json(
    *,
    url: str,
    payload: dict[str, Any],
    timeout_seconds: float,
    api_key: str | None = None,
) -> tuple[int, bytes, dict[str, Any] | None, str | None]:
    headers = {"Content-Type": "application/json"}
    if api_key is not None:
        headers["em_api_key"] = api_key
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout_seconds,
            context=ssl.create_default_context(),
        ) as response:
            status = int(response.status)
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, b"", None, f"{type(exc).__name__}: {exc}"

    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return status, raw, None, f"invalid_json: {exc}"
    if not isinstance(decoded, dict):
        return status, raw, None, "invalid_json_root"
    return status, raw, decoded, None


def _safe_summary(
    *,
    name: str,
    url: str,
    status: int,
    raw: bytes,
    decoded: dict[str, Any] | None,
    error: str | None,
    expected_asset_type: str | None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "name": name,
        "url": url,
        "http_status": status,
        "response_bytes": len(raw),
        "response_sha256": "sha256:" + hashlib.sha256(raw).hexdigest() if raw else None,
        "transport_error": error,
    }
    if decoded is not None:
        summary["provider_code"] = decoded.get("code")
        message = decoded.get("message") or decoded.get("msg")
        summary["provider_message"] = str(message)[:300] if message is not None else None
        data = decoded.get("data")
        summary["data_keys"] = sorted(str(key) for key in data) if isinstance(data, dict) else []
    if name.startswith("local_"):
        semantic_ok, semantic_error = _validate_local_response(
            name=name,
            decoded=decoded,
            expected_asset_type=expected_asset_type,
        )
        summary["semantic_ok"] = semantic_ok
        summary["semantic_error"] = semantic_error
    return summary


def _has_non_empty_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float, bool)):
        return True
    if isinstance(value, dict):
        return any(_has_non_empty_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_non_empty_value(item) for item in value)
    return False


def _valid_provenance(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    response_hash = value.get("response_sha256")
    retrieved_at = value.get("retrieved_at")
    schema_version = value.get("schema_version")
    if not isinstance(response_hash, str) or _SHA256_RE.fullmatch(response_hash) is None:
        return False
    if not isinstance(schema_version, str) or not schema_version.strip():
        return False
    if not isinstance(retrieved_at, str):
        return False
    try:
        parsed = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _tables_have_values(tables: Any) -> bool:
    if not isinstance(tables, list) or not tables:
        return False

    def payload_has_metric_values(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return _has_non_empty_value(payload)
        return any(
            key not in {"headName", "header", "headers"} and _has_non_empty_value(value)
            for key, value in payload.items()
        )

    return any(
        isinstance(table, dict)
        and (
            payload_has_metric_values(table.get("rawTable"))
            or payload_has_metric_values(table.get("table"))
        )
        for table in tables
    )


def _normalised_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    if _SECURITY_CODE_RE.fullmatch(code) is None:
        return None
    return code[:6]


def _validate_local_response(
    *,
    name: str,
    decoded: dict[str, Any] | None,
    expected_asset_type: str | None,
) -> tuple[bool, str | None]:
    if decoded is None:
        return False, "response_is_not_json_object"
    if name == "local_screen":
        if not isinstance(decoded.get("rows"), list) or not decoded["rows"]:
            return False, "screen_has_no_rows"
        if not _valid_provenance(decoded.get("provenance")):
            return False, "screen_provenance_is_invalid"
        return True, None
    if name == "local_finance":
        if not _tables_have_values(decoded.get("tables")):
            return False, "finance_tables_have_no_values"
        if not _valid_provenance(decoded.get("provenance")):
            return False, "finance_provenance_is_invalid"
        return True, None
    if name == "local_dialogue_finance":
        data = decoded.get("data")
        if not isinstance(data, dict) or data.get("kind") != "finance":
            return False, "dialogue_did_not_route_to_finance"
        if data.get("usage_scope") != "current_query_only":
            return False, "dialogue_usage_scope_is_not_current_only"
        if data.get("historical_backtest_eligible") is not False:
            return False, "dialogue_current_result_is_marked_historical"
        finance = data.get("finance")
        if not isinstance(finance, dict) or not _tables_have_values(finance.get("tables")):
            return False, "dialogue_finance_has_no_values"
        if not _valid_provenance(finance.get("provenance")):
            return False, "dialogue_finance_provenance_is_invalid"
        return True, None
    if name == "local_dialogue_screened_finance":
        data = decoded.get("data")
        if not isinstance(data, dict) or data.get("kind") != "screened_finance":
            return False, "dialogue_did_not_route_to_screened_finance"
        screened = data.get("screened_finance")
        if not isinstance(screened, dict):
            return False, "dialogue_screened_finance_is_missing"
        return _validate_local_response(
            name="local_screen_query",
            decoded=screened,
            expected_asset_type="A股",
        )
    if not name.startswith("local_screen_query"):
        return False, "unknown_local_probe"
    if decoded.get("usage_scope") != "current_query_only":
        return False, "usage_scope_is_not_current_only"
    if decoded.get("historical_backtest_eligible") is not False:
        return False, "current_result_is_marked_historical"
    screen = decoded.get("screen")
    if not isinstance(screen, dict):
        return False, "screen_result_is_missing"
    if expected_asset_type is not None and screen.get("asset_type") != expected_asset_type:
        return False, "screen_asset_type_does_not_match"
    if not isinstance(screen.get("rows"), list) or not screen["rows"]:
        return False, "screen_has_no_rows"
    if not _valid_provenance(screen.get("provenance")):
        return False, "screen_provenance_is_invalid"
    entities = decoded.get("entities")
    batches = decoded.get("batches")
    if not isinstance(entities, list) or not entities:
        return False, "screen_selected_no_entities"
    if not isinstance(batches, list) or len(batches) != (len(entities) + 4) // 5:
        return False, "finance_batch_count_does_not_match"
    entity_codes: set[str] = set()
    for entity in entities:
        if not isinstance(entity, dict):
            return False, "entity_is_not_an_object"
        code = _normalised_code(entity.get("code"))
        if code is None or not isinstance(entity.get("name"), str) or not entity["name"].strip():
            return False, "entity_identity_is_incomplete"
        entity_codes.add(code)
    covered_codes: set[str] = set()
    for batch in batches:
        if not isinstance(batch, dict):
            return False, "finance_batch_is_not_an_object"
        if not _tables_have_values(batch.get("tables")):
            return False, "finance_batch_has_no_values"
        if not _valid_provenance(batch.get("provenance")):
            return False, "finance_batch_provenance_is_invalid"
        for table in batch.get("tables", []):
            if not isinstance(table, dict):
                continue
            for raw_code in table.get("entityCodes", []):
                code = _normalised_code(raw_code)
                if code is not None:
                    covered_codes.add(code)
    if not entity_codes.issubset(covered_codes):
        return False, "finance_tables_do_not_cover_every_selected_entity"
    return True, None


def _write_private(path: Path, raw: bytes) -> None:
    path.write_bytes(raw)
    path.chmod(0o600)


def _run_probe(
    *,
    output_dir: Path,
    name: str,
    url: str,
    payload: dict[str, Any],
    timeout_seconds: float,
    api_key: str | None,
    expected_asset_type: str | None = None,
) -> dict[str, Any]:
    status, raw, decoded, error = _request_json(
        url=url,
        payload=payload,
        timeout_seconds=timeout_seconds,
        api_key=api_key,
    )
    if raw:
        _write_private(output_dir / f"{name}.json", raw)
    return _safe_summary(
        name=name,
        url=url,
        status=status,
        raw=raw,
        decoded=decoded,
        error=error,
        expected_asset_type=expected_asset_type,
    )


def _provider_payload(query: str, *, kind: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": query,
        "toolContext": {
            "callId": f"probe_{kind}_{uuid.uuid4().hex}",
            "userInfo": {"userId": "ashare-backtest-probe"},
        },
    }
    if kind == "screen":
        payload["selectType"] = "A股"
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe Eastmoney MX screener and finance-data interfaces safely."
    )
    parser.add_argument("--provider-base", default=DEFAULT_PROVIDER_BASE)
    parser.add_argument("--local-api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--skip-provider", action="store_true")
    parser.add_argument("--skip-local", action="store_true")
    parser.add_argument(
        "--include-funds",
        action="store_true",
        help="Also probe ordinary funds; excluded from the default A-share/ETF gate.",
    )
    parser.add_argument(
        "--require-local",
        action="store_true",
        help="Return a non-zero exit code when any local service probe fails.",
    )
    parser.add_argument("--timeout", type=float, default=35.0)
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args()

    if args.skip_provider and args.skip_local:
        parser.error("--skip-provider and --skip-local cannot be used together")
    api_key = None if args.skip_provider else _read_key(args.key_file)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")  # noqa: UP017
    output_dir = args.output_root / timestamp
    output_dir.mkdir(parents=True, mode=0o700)
    output_dir.chmod(0o700)

    provider_base = args.provider_base.rstrip("/")
    screen_query = "A股今日上涨，按成交额从高到低取前3；获取证券代码和证券简称"
    finance_query = "查询东方财富(300059.SZ)、同花顺(300033.SZ)；获取最新价、涨跌幅、换手率"
    results: list[dict[str, Any]] = []
    if not args.skip_provider:
        results.extend(
            [
                _run_probe(
                    output_dir=output_dir,
                    name="provider_screen",
                    url=f"{provider_base}/proxy/b/mcp/tool/selectSecurity",
                    payload=_provider_payload(screen_query, kind="screen"),
                    timeout_seconds=args.timeout,
                    api_key=api_key,
                ),
                _run_probe(
                    output_dir=output_dir,
                    name="provider_finance",
                    url=f"{provider_base}/proxy/b/mcp/tool/searchData",
                    payload=_provider_payload(finance_query, kind="finance"),
                    timeout_seconds=args.timeout,
                    api_key=api_key,
                ),
            ]
        )

    if not args.skip_local:
        local_base = args.local_api_base.rstrip("/")
        local_probes = [
            _run_probe(
                output_dir=output_dir,
                name="local_screen",
                url=f"{local_base}/api/v1/market/screen",
                payload={"query": screen_query, "asset_type": "A股"},
                timeout_seconds=args.timeout,
                api_key=None,
                expected_asset_type="A股",
            ),
            _run_probe(
                output_dir=output_dir,
                name="local_finance",
                url=f"{local_base}/api/v1/market/query",
                payload={"query": finance_query, "indicators": "最新价、涨跌幅、换手率"},
                timeout_seconds=args.timeout,
                api_key=None,
            ),
            _run_probe(
                output_dir=output_dir,
                name="local_screen_query",
                url=f"{local_base}/api/v1/market/screen-query",
                payload={
                    "screening_query": "A股今日上涨，按成交额从高到低取前3",
                    "asset_type": "A股",
                    "indicators": "最新价、涨跌幅、换手率",
                },
                timeout_seconds=args.timeout,
                api_key=None,
                expected_asset_type="A股",
            ),
            _run_probe(
                output_dir=output_dir,
                name="local_screen_query_etf",
                url=f"{local_base}/api/v1/market/screen-query",
                payload={
                    "screening_query": "ETF按成交额从高到低取前3；获取基金代码和基金简称",
                    "asset_type": "ETF",
                    "indicators": "最新价、涨跌幅、成交额",
                },
                timeout_seconds=args.timeout,
                api_key=None,
                expected_asset_type="ETF",
            ),
            _run_probe(
                output_dir=output_dir,
                name="local_dialogue_finance",
                url=f"{local_base}/api/v1/strategy-drafts",
                payload={
                    "utterance": "东方财富昨天的换手率是多少",
                    "as_of_date": "2026-09-03",
                },
                timeout_seconds=args.timeout,
                api_key=None,
            ),
            _run_probe(
                output_dir=output_dir,
                name="local_dialogue_screened_finance",
                url=f"{local_base}/api/v1/strategy-drafts",
                payload={
                    "utterance": (
                        "A股今日上涨，按成交额从高到低取前3；"
                        "获取最新价和换手率"
                    ),
                    "as_of_date": "2026-09-03",
                },
                timeout_seconds=args.timeout,
                api_key=None,
            ),
        ]

        if args.include_funds:
            local_probes.append(
                _run_probe(
                    output_dir=output_dir,
                    name="local_screen_query_fund",
                    url=f"{local_base}/api/v1/market/screen-query",
                    payload={
                        "screening_query": (
                            "新能源混合基金近一年收益排名前3；获取基金代码和基金简称"
                        ),
                        "asset_type": "基金",
                        "indicators": "最新单位净值、近一年收益率、基金规模",
                    },
                    timeout_seconds=args.timeout,
                    api_key=None,
                    expected_asset_type="基金",
                )
            )
        results.extend(local_probes)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
        "credential_source": "environment" if os.environ.get("EM_API_KEY") else "key_file",
        "credential_value_recorded": False,
        "results": results,
    }
    summary_path = output_dir / "summary.json"
    _write_private(
        summary_path,
        json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    latest_path = args.output_root / "LATEST"
    latest_path.write_text(str(output_dir) + "\n", encoding="utf-8")
    latest_path.chmod(0o600)

    print(f"Probe finished. Safe summary: {summary_path}")
    for item in results:
        semantic_ok = item.get("semantic_ok", True)
        state = (
            "OK"
            if (
                200 <= int(item["http_status"]) < 300
                and not item["transport_error"]
                and semantic_ok is True
            )
            else "FAIL"
        )
        print(
            f"- {item['name']}: {state}; HTTP {item['http_status']}; "
            f"provider_code={item.get('provider_code')}; "
            f"semantic_error={item.get('semantic_error')}; "
            f"sha256={item['response_sha256']}"
        )
    provider_ok = all(
        200 <= int(item["http_status"]) < 300
        and not item["transport_error"]
        and item.get("provider_code") in {0, 200, "0", "200", None}
        for item in results
        if str(item["name"]).startswith("provider_")
    )
    local_ok = all(
        200 <= int(item["http_status"]) < 300
        and not item["transport_error"]
        and item.get("semantic_ok") is True
        for item in results
        if str(item["name"]).startswith("local_")
    )
    return 0 if provider_ok and (not args.require_local or local_ok) else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Probe cannot start: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
