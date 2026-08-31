"""Headless responsive smoke test for the current card-based Mock journey."""

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Browser, Page, expect, sync_playwright

_explicit_artifacts = os.environ.get("SMOKE_ARTIFACTS_DIR")
_run_label = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
ARTIFACTS = (
    Path(_explicit_artifacts)
    if _explicit_artifacts
    else Path(__file__).resolve().parents[1]
    / "artifacts"
    / "mock-runs"
    / f"{_run_label}-{os.getpid()}"
)
WIDTHS = (320, 390, 768, 1280)
HOME_CAPITAL_PATTERN = re.compile(r"本金|初始资金|起始本金|100\s*万|1,000,000|1000000")


def prepare_artifact_directory() -> None:
    """Use a new empty directory so stale screenshots cannot pass review."""

    if ARTIFACTS.exists() and any(ARTIFACTS.iterdir()):
        raise AssertionError(f"refusing to mix smoke evidence with existing files: {ARTIFACTS}")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)


def viewport_height(width: int) -> int:
    if width <= 390:
        return 844
    if width <= 768:
        return 1024
    return 900


def assert_no_horizontal_overflow(page: Page) -> None:
    widths = page.evaluate(
        """() => ({
          scroll: document.documentElement.scrollWidth,
          client: document.documentElement.clientWidth,
        })"""
    )
    if widths["scroll"] > widths["client"]:
        offenders = page.evaluate(
            """() => [...document.querySelectorAll('*')]
              .map((element) => {
                const rect = element.getBoundingClientRect();
                return {
                  tag: element.tagName,
                  className: element.className,
                  left: rect.left,
                  right: rect.right,
                  width: rect.width,
                };
              })
              .filter((item) =>
                item.right > document.documentElement.clientWidth + 0.5 ||
                item.left < -0.5)
              .slice(0, 12)"""
        )
        raise AssertionError(f"horizontal overflow: {widths}; offenders: {offenders}")


def assert_chart_tooltip_clear_of_axis(page: Page, expected_placement: str) -> None:
    chart = page.locator("#report-performance .chart")
    tooltip = page.locator("#report-performance .tip.on")
    expect(tooltip).to_be_visible()
    expect(tooltip).to_have_attribute("data-placement", expected_placement)
    chart_box = chart.bounding_box()
    tooltip_box = tooltip.bounding_box()
    if chart_box is None or tooltip_box is None:
        raise AssertionError("chart or tooltip has no rendered bounding box")
    plot_left = float(chart.get_attribute("data-plot-left") or "0")
    view_width = float(chart.get_attribute("data-view-width") or "1")
    axis_right = chart_box["x"] + chart_box["width"] * plot_left / view_width
    if tooltip_box["x"] < axis_right + 1:
        raise AssertionError(
            f"chart tooltip overlaps y-axis: tooltip={tooltip_box}, axis_right={axis_right}"
        )
    tooltip_right = tooltip_box["x"] + tooltip_box["width"]
    chart_right = chart_box["x"] + chart_box["width"]
    if tooltip_right > chart_right + 1:
        raise AssertionError(
            f"chart tooltip exceeds chart: tooltip={tooltip_box}, chart={chart_box}"
        )


def assert_mock_identity(page: Page) -> None:
    expect(page.locator(".app")).to_have_attribute("data-api-mode", "mock")
    expect(page.locator(".mode-pill")).to_have_text("界面预览")
    expect(page.locator(".mode-pill")).to_be_visible()
    expect(page.get_by_text(re.compile(r"^proved$", re.IGNORECASE))).to_have_count(0)


def assert_home_hides_default_capital(page: Page) -> None:
    home = page.locator("#pg-chat")
    expect(home).to_be_visible()
    surface = home.evaluate(
        """(root) => [
          root.innerText,
          ...[...root.querySelectorAll('input, textarea, select')]
            .map((control) => control.value),
        ].join(' ')"""
    )
    if HOME_CAPITAL_PATTERN.search(surface):
        raise AssertionError(f"default capital leaked into #pg-chat: {surface!r}")


def screenshot(page: Page, name: str) -> None:
    page.wait_for_timeout(50)
    page.screenshot(path=str(ARTIFACTS / name), full_page=False)


def new_page(
    browser: Browser,
    errors: list[str],
    api_requests: list[str],
    width: int,
    scenario: str,
) -> Page:
    page = browser.new_page(
        viewport={"width": width, "height": viewport_height(width)},
        reduced_motion="reduce",
    )
    page.on(
        "console",
        lambda message: (
            errors.append(f"{scenario}@{width}: {message.text}")
            if message.type == "error"
            else None
        ),
    )
    page.on("pageerror", lambda error: errors.append(f"{scenario}@{width}: {error}"))
    page.on(
        "request",
        lambda request: (
            api_requests.append(f"{scenario}@{width}: {request.method} {request.url}")
            if urlparse(request.url).path.startswith("/api/")
            else None
        ),
    )
    return page


def open_initial(page: Page, base_url: str) -> None:
    page.goto(base_url, wait_until="networkidle")
    assert_mock_identity(page)
    expect(page.get_by_role("textbox", name="交易规则")).to_have_value(
        "东方财富 MACD 金叉买入，死叉卖出，回测近 5 年"
    )
    assert_home_hides_default_capital(page)
    assert_no_horizontal_overflow(page)


def run_technical(page: Page, base_url: str, width: int) -> None:
    open_initial(page, base_url)
    screenshot(page, f"initial-{width}.png")
    page.get_by_role("button", name="识别交易规则").click()
    expect(page.get_by_text("已完成思考", exact=True)).to_be_visible()
    expect(page.get_by_text("MACD 金叉", exact=True).first).to_be_visible()
    expect(page.get_by_text("MACD 死叉", exact=True).first).to_be_visible()
    assert_home_hides_default_capital(page)

    page.get_by_role("button", name=re.compile(r"^区间")).click()
    expect(page.get_by_role("heading", name="策略设置", exact=True)).to_be_visible()
    expect(page.get_by_role("spinbutton", name="初始资金")).to_have_value("1000000")
    page.get_by_role("button", name="完成", exact=True).click()
    assert_home_hides_default_capital(page)

    page.get_by_role("button", name="开始回测", exact=True).click()
    page.wait_for_function(
        """() => document.body.innerText.includes('演示：') ||
        document.body.innerText.includes('回测结果')"""
    )
    expect(page.get_by_text("下面显示的是后台返回的真实阶段。", exact=False)).to_have_count(0)
    expect(page.get_by_text("回测结果", exact=True)).to_be_visible(timeout=15_000)
    expect(page.get_by_role("group", name="可选的下一步")).to_be_visible()
    for label in ("换个条件再跑一次", "把这条设成盯盘提醒", "换只股票试试"):
        expect(page.get_by_role("button", name=label, exact=True)).to_be_visible()
    assert_mock_identity(page)
    assert_home_hides_default_capital(page)
    expect(page.get_by_role("button", name="回到底部", exact=True)).to_have_count(0)
    expect(
        page.get_by_text(
            "策略亏损 2.54%，同样的钱买入后一直持有亏损 39.23%，相对少亏 36.69 个百分点。",
            exact=True,
        )
    ).to_be_visible()
    expect(page.get_by_role("button", name="换个条件", exact=True)).to_have_count(0)
    screenshot(page, f"technical-result-{width}.png")

    page.get_by_role("button", name="查看完整报告", exact=True).click()
    expect(page.get_by_role("heading", name="回测报告", exact=True)).to_be_visible()
    assert_mock_identity(page)
    expect(page.locator("#pg-report").get_by_text("演示数据", exact=True)).to_have_count(0)
    expect(page.get_by_text(re.compile(r"^proved$", re.IGNORECASE))).to_have_count(0)
    report = page.locator("#pg-report")
    expect(report.get_by_text("初始资金", exact=True)).to_have_count(0)
    expect(report.get_by_text("期末资产", exact=True)).to_have_count(0)
    expect(report.get_by_role("tablist")).to_have_count(0)
    expect(page.get_by_role("heading", name="净值与回撤", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="每笔委托", exact=True)).to_be_visible()
    expect(page.locator("#pg-report .report-risk .notice")).to_have_count(0)
    expect(report.get_by_text(re.compile("风险提示"))).to_have_count(0)
    screenshot(page, f"technical-report-overview-{width}.png")

    page.locator("#pg-report button[data-back]").click()
    assert_home_hides_default_capital(page)
    page.get_by_role("button", name="查看完整报告", exact=True).click()
    expect(page.get_by_role("heading", name="回测报告", exact=True)).to_be_visible()

    expect(page.get_by_text("最大回撤", exact=False).first).to_be_visible()
    chart_points = page.locator("#report-performance").get_by_role(
        "button", name=re.compile("买入成交|卖出成交|未成交")
    )
    expect(chart_points.first).to_be_visible()
    chart_points.first.click()
    expect(page.locator("#report-trades .order-row[aria-current=true]")).to_be_visible()
    assert_chart_tooltip_clear_of_axis(page, "right")
    screenshot(page, f"technical-chart-point-{width}.png")
    chart_points.last.click()
    expect(page.locator("#report-trades .order-row[aria-current=true]")).to_be_visible()
    assert_chart_tooltip_clear_of_axis(page, "left")
    screenshot(page, f"technical-chart-point-flipped-{width}.png")

    blocked = page.locator("#report-trades").get_by_role(
        "button", name=re.compile("买入 MACD 金叉确认 未成")
    )
    expect(blocked).to_be_visible()
    blocked.click()
    expect(page.get_by_text("从你那句话到账户变化", exact=True)).to_be_visible()
    expect(page.get_by_text("用户原话", exact=True)).to_be_visible()
    expect(page.get_by_text("规范化条件", exact=True)).to_be_visible()
    chain = page.locator("#pg-chain")
    for label in ("MACD 金叉确认", "形成买入决策", "提交买入委托", "涨停未成交", "账户净值记录"):
        expect(chain.get_by_text(label, exact=True)).to_be_visible()
    expect(chain.get_by_text("技术详情", exact=True)).to_be_visible()
    expect(chain.get_by_text(re.compile(r"chain decision_buy_blocked")).first).not_to_be_visible()
    expect(chain.get_by_text(re.compile(r"activity\.decisionId"))).to_have_count(0)
    assert_mock_identity(page)
    assert_no_horizontal_overflow(page)
    screenshot(page, f"technical-chain-{width}.png")


def run_event(page: Page, base_url: str) -> None:
    open_initial(page, base_url)
    page.get_by_role("button", name="年报发布后买入", exact=True).click()
    expect(page.get_by_text("已完成思考", exact=True)).to_be_visible()
    expect(page.get_by_text("年度报告发布", exact=True).first).to_be_visible()
    expect(page.get_by_text("年度报告 参数", exact=True)).to_have_count(0)
    page.get_by_role("button", name="开始回测", exact=True).click()
    expect(page.get_by_text("回测结果", exact=True)).to_be_visible(timeout=15_000)
    assert_mock_identity(page)
    page.get_by_role("button", name="查看完整报告", exact=True).click()
    expect(page.get_by_role("heading", name="回测报告", exact=True)).to_be_visible()
    assert_mock_identity(page)
    expect(page.get_by_text(re.compile(r"^proved$", re.IGNORECASE))).to_have_count(0)
    page.get_by_role("button", name=re.compile("成交规则与数据依据")).click()
    expect(page.get_by_role("heading", name="成交规则与数据依据", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="信号来源与时间", exact=True)).to_be_visible()
    provider = page.get_by_text("演示样例（非真实公告）", exact=True)
    expect(provider).to_be_visible()
    expect(page.get_by_text(re.compile("供应商首次观测时间已校验"))).to_be_visible()
    expect(page.get_by_text("时间精度：可靠到秒；按实际秒可得", exact=True)).to_be_visible()
    expect(page.get_by_text(re.compile("仅用于界面演示，未连接真实事件数据"))).to_be_visible()
    expect(page.get_by_text("mock_sample", exact=True)).not_to_be_visible()
    expect(page.get_by_text("demonstration_only", exact=True)).not_to_be_visible()
    provider.scroll_into_view_if_needed()
    screenshot(page, "event-source-390.png")
    missing_identity = page.locator("#pg-execution .identity-missing")
    expect(missing_identity).to_contain_text("数据快照")
    expect(missing_identity).to_contain_text("演示数据不带真实版本与校验值")
    assert_no_horizontal_overflow(page)
    screenshot(page, "event-evidence-390.png")


def run_document_term_strategy(
    page: Page,
    base_url: str,
    width: int,
    *,
    complete_journey: bool,
) -> None:
    """Exercise the annual-report body-count rule without falling back to MACD."""

    utterance = "同花顺发年报提到ai次数超过5次的话就买入，3天后卖出"
    entry_rule = "年度报告正文中“AI”完整词出现 > 5 次"
    exit_rule = "实际买入成交后第 3 个交易日卖出"

    separator = "&" if "?" in base_url else "?"
    page.goto(f"{base_url}{separator}symbol=300033.SZ", wait_until="networkidle")
    assert_mock_identity(page)
    rule = page.get_by_role("textbox", name="交易规则")
    expect(rule).to_have_value("300033.SZ MACD 金叉买入，死叉卖出，回测近 5 年")
    assert_home_hides_default_capital(page)
    assert_no_horizontal_overflow(page)
    rule.fill(utterance)
    page.get_by_role("button", name="识别交易规则").click()

    expect(page.get_by_text("已完成思考", exact=True)).to_be_visible()
    expect(page.get_by_text(entry_rule, exact=True).first).to_be_visible()
    expect(page.get_by_text(exit_rule, exact=True).first).to_be_visible()
    # 说明保留在可展开的“已完成思考”内; Mock 继续由内部模式字段隔离,
    # 因此这里验证内容存在即可, 不额外增加首页说明卡.
    expect(page.get_by_text(re.compile(r"没有读取真实年报正文，也没有真实计算词频"))).to_have_count(
        1
    )
    expect(page.get_by_text("MACD 金叉", exact=True)).to_have_count(0)
    expect(page.get_by_text("MACD 死叉", exact=True)).to_have_count(0)
    assert_mock_identity(page)
    assert_home_hides_default_capital(page)
    assert_no_horizontal_overflow(page)

    if not complete_journey:
        return

    screenshot(page, f"document-term-strategy-{width}.png")
    page.get_by_role("button", name="开始回测", exact=True).click()
    expect(page.get_by_text("回测结果", exact=True)).to_be_visible(timeout=15_000)
    expect(page.get_by_text("接下来可以继续验证", exact=True)).to_have_count(0)
    expect(page.get_by_role("group", name="可选的下一步")).to_be_visible()
    for label in ("换个条件再跑一次", "把这条设成盯盘提醒", "换只股票试试"):
        expect(page.get_by_role("button", name=label, exact=True)).to_be_visible()
    expect(page.get_by_text("MACD 金叉", exact=True)).to_have_count(0)
    expect(page.get_by_text("MACD 死叉", exact=True)).to_have_count(0)
    assert_mock_identity(page)
    assert_no_horizontal_overflow(page)
    screenshot(page, f"document-term-result-{width}.png")

    page.get_by_role("button", name="查看完整报告", exact=True).click()
    expect(page.get_by_role("heading", name="回测报告", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="每笔委托", exact=True)).to_be_visible()
    expect(
        page.get_by_role("button", name=re.compile("买入 年度报告正文词频条件确认 已成"))
    ).to_be_visible()
    expect(
        page.get_by_role("button", name=re.compile("卖出 持有 3 个交易日退出 已成"))
    ).to_be_visible()
    expect(page.get_by_text("MACD 金叉确认", exact=True)).to_have_count(0)
    expect(page.get_by_text("MACD 死叉确认", exact=True)).to_have_count(0)
    assert_mock_identity(page)
    assert_no_horizontal_overflow(page)
    screenshot(page, f"document-term-trades-{width}.png")

    page.get_by_role("button", name=re.compile("买入 年度报告正文词频条件确认 已成")).click()
    expect(page.get_by_role("heading", name="买入成交 · 因果轨迹", exact=True)).to_be_visible()
    chain = page.locator("#pg-chain")
    expect(chain.get_by_text("从你那句话到账户变化", exact=True)).to_be_visible()
    expect(chain.get_by_text("用户原话", exact=True)).to_be_visible()
    expect(chain.get_by_text("规范化条件", exact=True)).to_be_visible()
    expect(chain.get_by_text(utterance, exact=True)).to_be_visible()
    expect(chain.get_by_text(re.compile(f"买入：{re.escape(entry_rule)}"))).to_be_visible()
    expect(chain.get_by_text(re.compile(f"卖出：{re.escape(exit_rule)}"))).to_be_visible()
    expect(chain.get_by_text("年度报告正文词频条件确认", exact=True)).to_be_visible()
    expect(chain.get_by_text("形成买入决策", exact=True)).to_be_visible()
    expect(chain.get_by_text("提交买入委托", exact=True)).to_be_visible()
    expect(chain.get_by_text("买入成交", exact=True)).to_be_visible()
    expect(chain.get_by_text("账户净值记录", exact=True)).to_be_visible()
    expect(chain.get_by_text("MACD 金叉确认", exact=True)).to_have_count(0)
    expect(chain.get_by_text("mock_sample", exact=True)).not_to_be_visible()
    expect(chain.get_by_text("demonstration_only", exact=True)).not_to_be_visible()
    assert_mock_identity(page)
    assert_no_horizontal_overflow(page)
    screenshot(page, f"document-term-chain-{width}.png")


def run_unsupported(page: Page, base_url: str) -> None:
    open_initial(page, base_url)
    rule = page.get_by_role("textbox", name="交易规则")
    rule.fill("火星逆行时满仓，月圆时卖出")
    page.get_by_role("button", name="识别交易规则").click()
    expect(page.get_by_text("无法识别这条策略", exact=True)).to_be_visible()
    expect(page.get_by_text("MACD 金叉", exact=True)).to_have_count(0)
    assert_mock_identity(page)
    assert_no_horizontal_overflow(page)
    screenshot(page, "unsupported-390.png")


def run_clarification(page: Page, base_url: str) -> None:
    open_initial(page, base_url)
    rule = page.get_by_role("textbox", name="交易规则")
    rule.fill("MACD")
    page.get_by_role("button", name="识别交易规则").click()
    expect(page.get_by_text("只问这一次", exact=True)).to_be_visible()
    expect(page.get_by_text("请一次写清什么时候买入、什么时候卖出。", exact=True)).to_be_visible()
    recommended = page.get_by_role("button", name=re.compile("补充完整规则"))
    expect(recommended).to_be_enabled()
    recommended.click()
    expect(rule).to_have_value("MACD")
    expect(rule).to_be_focused()
    expect(page.get_by_text("已完成思考", exact=True)).to_have_count(0)
    assert_mock_identity(page)
    assert_no_horizontal_overflow(page)
    screenshot(page, "clarification-390.png")


def main() -> None:
    prepare_artifact_directory()
    browser_errors: list[str] = []
    api_requests: list[str] = []
    base_url = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:5173")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for width in WIDTHS:
            page = new_page(browser, browser_errors, api_requests, width, "technical")
            run_technical(page, base_url, width)
            page.close()

        for width in WIDTHS:
            page = new_page(
                browser,
                browser_errors,
                api_requests,
                width,
                "document-term",
            )
            run_document_term_strategy(
                page,
                base_url,
                width,
                complete_journey=width == 390,
            )
            page.close()

        for scenario, journey in (
            ("event", run_event),
            ("unsupported", run_unsupported),
            ("clarification", run_clarification),
        ):
            page = new_page(browser, browser_errors, api_requests, 390, scenario)
            journey(page, base_url)
            page.close()
        browser.close()

    if browser_errors:
        raise AssertionError(f"browser errors: {browser_errors}")
    if api_requests:
        raise AssertionError(f"Mock journeys made backend API requests: {api_requests}")
    print(
        "responsive Mock technical and annual-report text-count journeys passed "
        f"at 320/390/768/1280px; screenshots: {ARTIFACTS}"
    )


if __name__ == "__main__":
    main()
