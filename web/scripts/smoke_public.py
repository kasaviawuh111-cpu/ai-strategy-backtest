"""Public HTTPS browser smoke for the Live A-share backtest H5.

This is deliberately separate from ``smoke_live.py``.  The latter protects the
isolated local integration port; this script proves that an already deployed
HTTPS frontend talks to an already deployed HTTPS FastAPI service.  A static
SPA fallback returning ``200 text/html`` for ``/api/v1/*`` is an immediate
failure, not a healthy API response.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Page, Response, expect, sync_playwright
from smoke_live import (
    assert_no_horizontal_overflow,
    assert_real_api_mode,
    prepare_artifact_directory,
    run_journey,
    validate_expected_producer_snapshot_id,
    viewport_height,
)

DEFAULT_WIDTHS = (320, 390, 768, 1280)
JSON_CONTENT_TYPES = frozenset({"application/json", "application/problem+json"})
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frontend-url", required=True, help="public HTTPS H5 origin")
    parser.add_argument("--api-url", required=True, help="public HTTPS FastAPI origin")
    parser.add_argument(
        "--expected-producer-snapshot-id",
        required=True,
        help="exact immutable Composite v2 producer expected in both runs",
    )
    parser.add_argument("--width", action="append", type=int, dest="widths")
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument(
        "--backend-log-file",
        type=Path,
        help="optional exported backend log; when supplied every request ID must occur in it",
    )
    args = parser.parse_args()
    try:
        args.frontend_url = validate_public_origin(args.frontend_url, "frontend")
        args.api_url = validate_public_origin(args.api_url, "API")
        args.expected_producer_snapshot_id = validate_expected_producer_snapshot_id(
            args.expected_producer_snapshot_id
        )
    except (argparse.ArgumentTypeError, AssertionError) as exc:
        parser.error(str(exc))
    args.widths = tuple(args.widths or DEFAULT_WIDTHS)
    if any(not 320 <= width <= 1920 for width in args.widths):
        parser.error("viewport width must be within 320—1920")
    if args.timeout_seconds <= 0:
        parser.error("timeout must be positive")
    return args


def validate_public_origin(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise argparse.ArgumentTypeError(f"{label} URL is required")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise argparse.ArgumentTypeError(f"{label} URL must be an absolute HTTPS origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError(f"{label} URL cannot contain credentials/query/fragment")
    if parsed.path not in {"", "/"}:
        raise argparse.ArgumentTypeError(f"{label} URL must not contain a path")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        raise argparse.ArgumentTypeError(f"{label} URL cannot use localhost or .local")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise argparse.ArgumentTypeError(f"{label} URL cannot use a private/local IP")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"https://{hostname}{port}"


def artifact_directory(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return Path(__file__).resolve().parents[1] / "artifacts" / "public-runs" / stamp


def _content_type(headers: Mapping[str, str]) -> str:
    return headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _request_json(
    url: str,
    *,
    origin: str,
    timeout: float,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, str], int]:
    request_headers = {
        "Accept": "application/json, application/problem+json",
        "Origin": origin,
        "X-Request-ID": "public-smoke-preflight",
        **(dict(headers or {})),
    }
    request = urllib.request.Request(url, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
            context=ssl.create_default_context(),
        ) as response:
            status = response.status
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            body = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        response_headers = {key.lower(): value for key, value in exc.headers.items()}
        body = exc.read()
    content_type = _content_type(response_headers)
    if content_type not in JSON_CONTENT_TYPES:
        preview = body[:160].decode("utf-8", errors="replace")
        raise AssertionError(
            f"{method} {url} returned {status} {content_type or '<missing>'}; "
            f"expected JSON, possible SPA/static fallback: {preview!r}"
        )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{method} {url} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AssertionError(f"{method} {url} did not return a JSON object")
    return cast(dict[str, Any], payload), response_headers, status


def run_preflight(frontend: str, api: str, timeout: float) -> dict[str, Any]:
    ready, ready_headers, ready_status = _request_json(
        urljoin(api + "/", "api/v1/ready"), origin=frontend, timeout=timeout
    )
    if ready_status != 200 or ready.get("status") != "ready":
        raise AssertionError(f"public strict readiness failed: HTTP {ready_status} {ready!r}")
    capabilities, capability_headers, capability_status = _request_json(
        urljoin(api + "/", "api/v1/capabilities"), origin=frontend, timeout=timeout
    )
    if capability_status != 200:
        raise AssertionError(f"public capabilities returned HTTP {capability_status}")
    if capabilities.get("capability_source") == "mock_demo":
        raise AssertionError("public capabilities still identify mock_demo")
    if capabilities.get("backtest_execution_available") is not True:
        raise AssertionError("public API says backtest execution is unavailable")
    for label, headers in (
        ("ready", ready_headers),
        ("capabilities", capability_headers),
    ):
        request_id = headers.get("x-request-id")
        if not request_id or REQUEST_ID_RE.fullmatch(request_id) is None:
            raise AssertionError(f"public {label} response has no valid X-Request-ID")

    cors: dict[str, Any] = {"required": frontend != api, "verified": frontend == api}
    if frontend != api:
        request = urllib.request.Request(
            urljoin(api + "/", "api/v1/strategy-drafts"),
            method="OPTIONS",
            headers={
                "Origin": frontend,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": (
                    "accept,content-type,idempotency-key,x-request-id"
                ),
            },
        )
        with urllib.request.urlopen(
            request,
            timeout=timeout,
            context=ssl.create_default_context(),
        ) as response:
            cors_headers = {key.lower(): value for key, value in response.headers.items()}
            if response.status not in {200, 204}:
                raise AssertionError(f"CORS preflight returned HTTP {response.status}")
        if cors_headers.get("access-control-allow-origin") != frontend:
            raise AssertionError("CORS does not allow the exact public frontend origin")
        allowed_methods = {
            item.strip().upper()
            for item in cors_headers.get("access-control-allow-methods", "").split(",")
            if item.strip()
        }
        required_methods = {"GET", "POST", "OPTIONS"}
        if not required_methods <= allowed_methods:
            missing = sorted(required_methods - allowed_methods)
            raise AssertionError(f"CORS is missing required methods: {missing!r}")
        allowed_headers = {
            item.strip().lower()
            for item in cors_headers.get("access-control-allow-headers", "").split(",")
            if item.strip()
        }
        required_headers = {
            "accept",
            "content-type",
            "idempotency-key",
            "x-request-id",
        }
        if not required_headers <= allowed_headers:
            missing = sorted(required_headers - allowed_headers)
            raise AssertionError(f"CORS is missing required headers: {missing!r}")
        cors = {
            "required": True,
            "verified": True,
            "allowOrigin": cors_headers.get("access-control-allow-origin"),
            "allowMethods": cors_headers.get("access-control-allow-methods"),
            "allowHeaders": cors_headers.get("access-control-allow-headers"),
        }

    return {
        "frontendOrigin": frontend,
        "apiOrigin": api,
        "ready": ready,
        "capabilities": capabilities,
        "cors": cors,
        "requestIds": [
            ready_headers.get("x-request-id"),
            capability_headers.get("x-request-id"),
        ],
    }


class NetworkRecorder:
    def __init__(self, api_origin: str, output: Path) -> None:
        self.api_origin = api_origin
        self.output = output
        self.records: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.request_ids: set[str] = set()
        self._sequence = 0

    def attach(self, page: Page) -> None:
        page.on("response", self._capture)

    def _capture(self, response: Response) -> None:
        parsed = urlparse(response.url)
        if not parsed.path.startswith("/api/v1/"):
            return
        self._sequence += 1
        origin = f"{parsed.scheme}://{parsed.netloc}"
        content_type = _content_type(response.headers)
        request_id = response.headers.get("x-request-id")
        record: dict[str, Any] = {
            "sequence": self._sequence,
            "method": response.request.method,
            "url": response.url,
            "path": parsed.path,
            "status": response.status,
            "contentType": content_type,
            "requestId": request_id,
        }
        self.records.append(record)
        if origin != self.api_origin:
            self.errors.append(f"API call escaped configured public API origin: {response.url}")
        if response.status == 200 and content_type == "text/html":
            self.errors.append(f"API returned 200 text/html SPA fallback: {response.url}")
        if content_type not in JSON_CONTENT_TYPES:
            self.errors.append(
                f"API returned non-JSON content type {content_type or '<missing>'}: {response.url}"
            )
        if not request_id or REQUEST_ID_RE.fullmatch(request_id) is None:
            self.errors.append(f"API response has no valid X-Request-ID: {response.url}")
        else:
            self.request_ids.add(request_id)
        try:
            payload = response.json()
        except Exception as exc:
            self.errors.append(f"API response is not decodable JSON: {response.url}: {exc}")
            return
        body_path = self.output / f"api-{self._sequence:03d}.json"
        body_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        record["bodyFile"] = body_path.name

    def assert_complete(self) -> None:
        required = {
            "POST /api/v1/strategy-drafts",
            "POST /api/v1/backtest-runs",
        }
        observed = {f"{item['method']} {item['path']}" for item in self.records}
        missing = sorted(required - observed)
        if missing:
            self.errors.append(f"missing browser API calls: {missing}")
        for suffix in ("/summary", "/series", "/trades"):
            if not any(item["path"].endswith(suffix) for item in self.records):
                self.errors.append(f"missing browser result call ending {suffix}")
        if self.errors:
            raise AssertionError("; ".join(self.errors))


def run_failure_journey(page: Page, frontend: str, width: int, artifacts: Path) -> None:
    page.goto(frontend, wait_until="networkidle")
    assert_real_api_mode(page)
    input_box = page.get_by_role("textbox", name="交易规则")
    input_box.fill("火星逆行时满仓，月圆时卖出")
    page.get_by_role("button", name="识别交易规则").click()
    expect(page.get_by_text("无法识别这条策略", exact=True)).to_be_visible(timeout=20_000)
    expect(page.get_by_text("演示数据", exact=True)).to_have_count(0)
    assert_no_horizontal_overflow(page, width)
    page.screenshot(path=str(artifacts / f"public-unsupported-{width}.png"), full_page=False)


def verify_backend_logs(log_file: Path, request_ids: set[str]) -> None:
    body = log_file.read_text(encoding="utf-8", errors="replace")
    missing = sorted(request_id for request_id in request_ids if request_id not in body)
    if missing:
        raise AssertionError(f"backend log is missing browser request IDs: {missing}")


def main() -> None:
    args = parse_args()
    artifacts = artifact_directory(args.artifacts)
    prepare_artifact_directory(artifacts)
    preflight = run_preflight(args.frontend_url, args.api_url, args.timeout_seconds)
    (artifacts / "preflight.json").write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    browser_errors: list[str] = []
    all_records: list[dict[str, Any]] = []
    all_request_ids = {
        value for value in preflight["requestIds"] if isinstance(value, str) and value
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for strategy in ("technical", "event"):
            for width in args.widths:
                label = f"{strategy}-{width}"
                context = browser.new_context(
                    viewport={"width": width, "height": viewport_height(width)},
                    record_har_path=str(artifacts / f"{label}.har"),
                    record_har_content="embed",
                )
                page = context.new_page()
                recorder = NetworkRecorder(args.api_url, artifacts / label)
                (artifacts / label).mkdir()
                recorder.attach(page)
                page.on(
                    "console",
                    lambda message, label=label: (
                        browser_errors.append(f"{label}: console {message.text}")
                        if message.type == "error"
                        else None
                    ),
                )
                page.on(
                    "pageerror",
                    lambda error, label=label: browser_errors.append(f"{label}: {error}"),
                )
                run_journey(
                    page,
                    strategy,
                    width,
                    args.frontend_url,
                    artifacts,
                    args.expected_producer_snapshot_id,
                )
                recorder.assert_complete()
                all_records.extend(recorder.records)
                all_request_ids.update(recorder.request_ids)
                context.close()

        failure_context = browser.new_context(
            viewport={"width": 390, "height": viewport_height(390)},
            record_har_path=str(artifacts / "unsupported-390.har"),
            record_har_content="embed",
        )
        failure_page = failure_context.new_page()
        failure_recorder = NetworkRecorder(args.api_url, artifacts / "unsupported-390")
        (artifacts / "unsupported-390").mkdir()
        failure_recorder.attach(failure_page)
        run_failure_journey(failure_page, args.frontend_url, 390, artifacts)
        if any(
            item["method"] == "POST" and item["path"] == "/api/v1/backtest-runs"
            for item in failure_recorder.records
        ):
            raise AssertionError("unsupported strategy incorrectly created a backtest run")
        if failure_recorder.errors:
            raise AssertionError("; ".join(failure_recorder.errors))
        all_records.extend(failure_recorder.records)
        all_request_ids.update(failure_recorder.request_ids)
        failure_context.close()
        browser.close()

    if browser_errors:
        raise AssertionError(f"browser errors: {browser_errors}")
    request_manifest = {
        "schemaVersion": "ashare-lab.public-live-network-evidence.v1",
        "frontendOrigin": args.frontend_url,
        "apiOrigin": args.api_url,
        "producerSnapshotId": args.expected_producer_snapshot_id,
        "requestIds": sorted(all_request_ids),
        "responses": all_records,
        "backendLogsVerified": args.backend_log_file is not None,
    }
    (artifacts / "request-ids-and-network.json").write_text(
        json.dumps(request_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if args.backend_log_file is not None:
        verify_backend_logs(args.backend_log_file, all_request_ids)
        log_status = "backend request-ID log correlation passed"
    else:
        log_status = "backend log correlation pending; this is not final public proof"
    print(
        f"public HTTPS technical + event + unsupported journeys passed; {log_status}; "
        f"evidence: {artifacts}"
    )


if __name__ == "__main__":
    main()
