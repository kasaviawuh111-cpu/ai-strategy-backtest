"""Headless responsive smoke test for the current H5 against a real API.

This script is intentionally not a backend fixture.  It only passes when the
frontend was built with ``VITE_USE_MOCK=false`` and a real API completes the
selected technical/event run.
"""

import argparse
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page, Response, expect, sync_playwright

DEFAULT_WIDTHS = (320, 390, 768, 1280)
DEFAULT_BASE_URL = "http://127.0.0.1:5184"
COMPOSITE_SCHEMA_V2 = "ashare-lab.composite-research-snapshot.v2"
MARKET_DATA_SCHEMA_V3 = "local-parquet.market-data.v3"
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RAW_SHA256_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
COMPOSITE_ID_RE = re.compile(r"^composite:[0-9a-f]{64}$")
ASIA_SHANGHAI_SECOND_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+08:00$")


def selected_options() -> tuple[tuple[str, ...], tuple[int, ...], str, str]:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strategy",
        choices=("technical", "event", "all"),
        default=os.environ.get("ASHARE_SMOKE_STRATEGY", "all"),
    )
    parser.add_argument(
        "--width",
        action="append",
        type=int,
        dest="widths",
        help="viewport width; repeat to test multiple widths (default: 320/390/768/1280)",
    )
    parser.add_argument(
        "--expected-producer-snapshot-id",
        default=os.environ.get("ASHARE_EXPECTED_PRODUCER_SNAPSHOT_ID"),
        help="exact immutable Composite producer required in every Live result",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SMOKE_BASE_URL", DEFAULT_BASE_URL),
        help=f"isolated local Live H5 URL (required: {DEFAULT_BASE_URL})",
    )
    args = parser.parse_args()
    widths = tuple(args.widths or DEFAULT_WIDTHS)
    if any(not 320 <= width <= 1920 for width in widths):
        parser.error("viewport width must be within 320—1920")
    try:
        expected_producer_snapshot_id = validate_expected_producer_snapshot_id(
            args.expected_producer_snapshot_id
        )
    except AssertionError as exc:
        parser.error(str(exc))
    try:
        base_url = validate_live_base_url(args.base_url)
    except AssertionError as exc:
        parser.error(str(exc))
    strategies = ("technical", "event") if args.strategy == "all" else (args.strategy,)
    return strategies, widths, expected_producer_snapshot_id, base_url


def viewport_height(width: int) -> int:
    if width <= 390:
        return 844
    if width <= 768:
        return 1024
    return 900


def assert_no_horizontal_overflow(page: Page, width: int) -> None:
    dimensions = cast(
        dict[str, int],
        page.evaluate(
            """() => ({
          clientWidth: document.documentElement.clientWidth,
          scrollWidth: document.documentElement.scrollWidth,
        })"""
        ),
    )
    if dimensions["scrollWidth"] > dimensions["clientWidth"] + 1:
        raise AssertionError(f"horizontal overflow at {width}px: {dimensions}")


def assert_real_api_mode(page: Page) -> None:
    expect(page.locator(".app")).to_have_attribute("data-api-mode", "live")
    expect(page.get_by_text("演示数据", exact=True)).to_have_count(0)


def capture_result_payloads(page: Page) -> tuple[dict[str, Any], list[str]]:
    """Capture full result payloads whose hashes are shortened in the UI."""

    payloads: dict[str, Any] = {}
    errors: list[str] = []

    def capture(response: Response) -> None:
        path = urlparse(response.url).path
        endpoint = next(
            (
                name
                for name in ("summary", "trades")
                if re.fullmatch(rf"/api/v1/backtest-runs/[^/]+/{name}", path)
            ),
            None,
        )
        if endpoint is None or response.request.method != "GET":
            return
        if not response.ok:
            errors.append(f"{endpoint} returned HTTP {response.status}")
            return
        try:
            payloads[endpoint] = response.json()
        except Exception as exc:
            errors.append(f"could not decode {endpoint} response: {exc}")

    page.on("response", capture)
    return payloads, errors


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AssertionError(f"real run evidence is missing: {label}")
    return value


def assert_hash(value: Any, label: str, pattern: re.Pattern[str] = SHA256_RE) -> str:
    identity = require_string(value, label)
    if pattern.fullmatch(identity) is None:
        raise AssertionError(f"invalid {label}: {identity!r}")
    return identity


def validate_expected_producer_snapshot_id(value: Any) -> str:
    identity = require_string(value, "expected producer snapshot ID")
    if COMPOSITE_ID_RE.fullmatch(identity) is None:
        raise AssertionError(
            "expected producer snapshot ID must be composite:<64 lowercase hex digits>"
        )
    return identity


def validate_live_base_url(value: Any) -> str:
    if not isinstance(value, str) or value != DEFAULT_BASE_URL:
        raise AssertionError(
            f"Live H5 base URL must be exactly {DEFAULT_BASE_URL!r}; "
            "port 5173 and non-loopback URLs are protected from this smoke"
        )
    return value


def default_artifact_directory() -> Path:
    run_label = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return (
        Path(__file__).resolve().parents[1]
        / "artifacts"
        / "live-runs"
        / f"{run_label}-{os.getpid()}"
    )


def prepare_artifact_directory(artifacts: Path) -> None:
    """Reject stale evidence instead of mixing screenshots from separate runs."""

    if artifacts.exists() and any(artifacts.iterdir()):
        raise AssertionError(
            f"refusing to mix Live smoke evidence with existing files: {artifacts}"
        )
    artifacts.mkdir(parents=True, exist_ok=True)


def assert_expected_producer_snapshot(
    evidence: dict[str, Any],
    expected_producer_snapshot_id: str,
) -> str:
    expected = validate_expected_producer_snapshot_id(expected_producer_snapshot_id)
    actual = validate_expected_producer_snapshot_id(evidence.get("producerSnapshotId"))
    if actual != expected:
        raise AssertionError(
            "Live result used an unexpected producer snapshot: "
            f"expected {expected!r}, got {actual!r}"
        )
    return actual


def evidence_row(page: Page, label: str) -> Locator:
    row = page.get_by_text(label, exact=True).locator("xpath=..")
    expect(row).to_be_visible()
    return row


def assert_recorded_evidence(
    page: Page,
    payloads: dict[str, Any],
    expected_producer_snapshot_id: str,
) -> None:
    summary = payloads.get("summary")
    if not isinstance(summary, dict):
        raise AssertionError("Live smoke did not observe the summary response")
    typed_summary = cast(dict[str, Any], summary)
    raw_evidence = typed_summary.get("runEvidence")
    if not isinstance(raw_evidence, dict):
        raise AssertionError("summary.runEvidence is missing")
    evidence = cast(dict[str, Any], raw_evidence)

    git_sha = require_string(evidence.get("codeRevision"), "Git SHA")
    if GIT_SHA_RE.fullmatch(git_sha) is None:
        raise AssertionError(f"Git SHA must be exactly 40 lowercase hex: {git_sha!r}")
    snapshot_id = require_string(evidence.get("dataSnapshotId"), "Snapshot")
    checksum = assert_hash(evidence.get("dataSnapshotChecksum"), "Snapshot checksum")
    data_schema = require_string(evidence.get("dataSchemaVersion"), "Data schema")
    if data_schema != MARKET_DATA_SCHEMA_V3:
        raise AssertionError(f"Data schema must be {MARKET_DATA_SCHEMA_V3!r}, got {data_schema!r}")
    producer_schema = require_string(
        evidence.get("producerSnapshotSchemaVersion"), "Producer schema"
    )
    if producer_schema != COMPOSITE_SCHEMA_V2:
        raise AssertionError(
            f"Producer schema must be {COMPOSITE_SCHEMA_V2!r}, got {producer_schema!r}"
        )
    producer_snapshot_id = assert_expected_producer_snapshot(
        evidence,
        expected_producer_snapshot_id,
    )
    catalog_hash = assert_hash(evidence.get("catalogHash"), "Catalog hash")
    strategy_hash = assert_hash(evidence.get("strategyHash"), "Strategy hash")

    for label, value in (
        ("代码版本", git_sha),
        ("数据快照", snapshot_id),
        ("数据结构版本", data_schema),
        ("快照生成结构版本", producer_schema),
        ("生产快照", producer_snapshot_id),
        ("快照校验值", checksum),
        ("规则目录校验值", catalog_hash),
        ("策略校验值", strategy_hash),
    ):
        expect(evidence_row(page, label)).to_contain_text(value)


def assert_event_evidence(page: Page, payloads: dict[str, Any]) -> None:
    activities = payloads.get("trades")
    if not isinstance(activities, list):
        raise AssertionError("Live smoke did not observe the trades response")

    candidates: list[dict[str, Any]] = []
    for raw_activity in cast(list[Any], activities):
        if not isinstance(raw_activity, dict):
            continue
        activity = cast(dict[str, Any], raw_activity)
        raw_evidence = activity.get("evidence")
        if not isinstance(raw_evidence, list):
            continue
        for raw_item in cast(list[Any], raw_evidence):
            if not isinstance(raw_item, dict):
                continue
            item = cast(dict[str, Any], raw_item)
            if item.get("sourceEventId"):
                candidates.append(item)
    if not candidates:
        raise AssertionError("event run has no source event evidence")

    failures: list[str] = []
    for item in candidates:
        try:
            provider = require_string(item.get("provider"), "event provider")
            if provider == "mock_sample":
                raise AssertionError("event provider is still mock_sample")
            available_at = require_string(item.get("availableAt"), "event availableAt")
            if ASIA_SHANGHAI_SECOND_RE.fullmatch(available_at) is None:
                raise AssertionError(
                    "event availableAt must include seconds and the Asia/Shanghai +08:00 offset: "
                    f"{available_at!r}"
                )
            time_quality = require_string(item.get("timeQuality"), "event timeQuality")
            if time_quality not in {"exact", "vendor_observed"}:
                raise AssertionError(f"event timeQuality is not strict: {time_quality!r}")
            validation_status = require_string(
                item.get("validationStatus"), "event validationStatus"
            )
            if validation_status != "validated":
                raise AssertionError(
                    f"event validationStatus must be 'validated': {validation_status!r}"
                )
            source_url = require_string(item.get("sourceUrl"), "event source URL")
            parsed_url = urlparse(source_url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise AssertionError(f"event source URL is invalid: {source_url!r}")
            raw_hash = assert_hash(
                item.get("rawResponseSha256"),
                "event raw response SHA-256",
                RAW_SHA256_RE,
            )
        except AssertionError as exc:
            failures.append(str(exc))
            continue

        matching_links = [
            link
            for link in page.get_by_role("link").all()
            if link.get_attribute("href") == source_url
        ]
        if not matching_links:
            raise AssertionError(
                f"event source URL is present in the API but not linked in the UI: {source_url}"
            )
        expect(matching_links[0]).to_be_visible()
        provider_text = matching_links[0].inner_text().strip()
        if not provider_text or "未记录" in provider_text:
            raise AssertionError(
                f"event provider is present in the API but not rendered in the UI: {provider!r}"
            )
        expect(page.get_by_text(f"首次可得：{available_at}", exact=True).first).to_be_visible()
        expected_quality = "秒级精确时间" if time_quality == "exact" else "供应商秒级首次可得"
        expect(page.get_by_text(re.compile(re.escape(expected_quality))).first).to_be_visible()
        visible_hash = raw_hash if len(raw_hash) <= 26 else f"{raw_hash[:14]}…{raw_hash[-8:]}"
        expect(
            page.get_by_text(re.compile(rf"响应\s+{re.escape(visible_hash)}")).first
        ).to_be_visible()
        return

    raise AssertionError("no event evidence passed strict validation: " + "; ".join(failures))


def run_journey(
    page: Page,
    strategy: str,
    width: int,
    base_url: str,
    artifacts: Path,
    expected_producer_snapshot_id: str,
) -> None:
    payloads, payload_errors = capture_result_payloads(page)
    page.goto(base_url, wait_until="networkidle")
    assert_real_api_mode(page)
    expect(page.get_by_role("textbox", name="交易规则")).to_be_visible()

    if strategy == "event":
        page.get_by_role("textbox", name="交易规则").fill(
            "东方财富年度报告发布后买入，MACD 死叉卖出，回测近 5 年"
        )
    page.get_by_role("button", name="识别交易规则").click()

    expect(page.get_by_text("已读懂你的规则", exact=True)).to_be_visible(timeout=20_000)
    if strategy == "event":
        expect(page.get_by_text("年度报告发布", exact=True).first).to_be_visible()
        expect(page.get_by_text("年度报告 参数", exact=True)).to_have_count(0)
    else:
        expect(page.get_by_text("MACD 金叉", exact=True).first).to_be_visible()
        expect(page.get_by_text("MACD 死叉", exact=True).first).to_be_visible()

    page.get_by_role("button", name="开始回测", exact=True).click()
    expect(page.get_by_text("跑完了，结果在下面。", exact=True)).to_be_visible(timeout=120_000)
    assert_real_api_mode(page)
    expect(
        page.get_by_text(
            re.compile(
                r"^策略(?:盈利|亏损|收益) .+，同资金买入持有(?:盈利|亏损|收益) .+，"
                r"(?:相对少亏|相对多亏|相对领先|相对落后|相对持平) .+ 个百分点。$"
            )
        ).first
    ).to_be_visible()
    expect(page.get_by_role("button", name="换个条件", exact=True)).to_have_count(0)

    page.get_by_role("button", name="查看完整报告", exact=True).click()
    expect(page.get_by_role("heading", name="回测报告", exact=True)).to_be_visible()
    expect(page.get_by_text("身份已记录", exact=True)).to_be_visible()
    assert_real_api_mode(page)

    report = page.locator("#pg-report")
    expect(report.get_by_text("初始资金", exact=True)).to_have_count(0)
    expect(report.get_by_text("期末资产", exact=True)).to_have_count(0)
    expect(report.get_by_role("tablist")).to_have_count(0)
    expect(page.get_by_role("heading", name="净值与回撤", exact=True)).to_be_visible()
    expect(page.get_by_text("最大回撤", exact=True).first).to_be_visible()
    expect(page.locator("#pg-report .report-risk .notice")).to_have_count(0)
    expect(report.get_by_text(re.compile("风险提示"))).to_have_count(0)

    expect(page.get_by_role("heading", name="每笔买卖", exact=True)).to_be_visible()
    activity = page.locator("button.trade").first
    expect(activity).to_be_visible()
    activity.click()
    expect(page.get_by_text("从你那句话到账户变化", exact=True)).to_be_visible()
    expect(page.get_by_text("用户原话", exact=True)).to_be_visible()
    expect(page.get_by_text("规范化条件", exact=True)).to_be_visible()
    page.get_by_role("button", name="返回", exact=True).click()

    page.get_by_role("button", name=re.compile("成交规则与数据依据")).click()
    expect(page.get_by_role("heading", name="成交规则与数据依据", exact=True)).to_be_visible()
    assert_recorded_evidence(page, payloads, expected_producer_snapshot_id)
    if strategy == "event":
        assert_event_evidence(page, payloads)
    if payload_errors:
        raise AssertionError(f"Live result capture errors: {payload_errors}")

    assert_no_horizontal_overflow(page, width)
    artifacts.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(artifacts / f"live-{strategy}-{width}.png"),
        full_page=False,
    )


def main() -> None:
    strategies, widths, expected_producer_snapshot_id, base_url = selected_options()
    explicit_artifacts = os.environ.get("SMOKE_ARTIFACTS_DIR")
    artifacts = Path(explicit_artifacts) if explicit_artifacts else default_artifact_directory()
    prepare_artifact_directory(artifacts)
    browser_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for strategy in strategies:
            for width in widths:
                page = browser.new_page(viewport={"width": width, "height": viewport_height(width)})
                page.on(
                    "console",
                    lambda message, strategy=strategy, width=width: (
                        browser_errors.append(f"{strategy}@{width}: {message.text}")
                        if message.type == "error"
                        else None
                    ),
                )
                page.on(
                    "pageerror",
                    lambda error, strategy=strategy, width=width: browser_errors.append(
                        f"{strategy}@{width}: {error}"
                    ),
                )
                run_journey(
                    page,
                    strategy,
                    width,
                    base_url,
                    artifacts,
                    expected_producer_snapshot_id,
                )
                page.close()
        browser.close()

    if browser_errors:
        raise AssertionError(f"browser errors: {browser_errors}")
    joined = ", ".join(str(width) for width in widths)
    print(
        f"real API H5 journeys {strategies} passed at {joined}px without "
        f"horizontal overflow or browser errors; screenshots: {artifacts}"
    )


if __name__ == "__main__":
    main()
