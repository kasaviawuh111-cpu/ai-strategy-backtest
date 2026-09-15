"""Exercise the real local UI through generation, execution, and assumption review."""

import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8093"
out = Path(__file__).parent / (
    sys.argv[2] if len(sys.argv) > 2 else "ui-execution-local"
)
out.mkdir(exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    responses: list[dict[str, object]] = []

    def capture(response):
        if "/api/v1/" not in response.url:
            return
        responses.append({"url": response.url, "http": response.status})

    page.on("response", capture)
    page.goto(base)
    box = page.get_by_role("textbox", name="交易规则")
    # CloudBase's test-domain notice loads asynchronously and has a countdown.
    # Wait for either real entry state before deciding which journey to follow.
    notice = page.get_by_role("button", name=re.compile(r"^确定访问"))
    box.or_(notice).first.wait_for(timeout=30_000)
    if notice.count():
        page.get_by_role("button", name="确定访问", exact=True).click(timeout=15_000)
    box.wait_for(timeout=30_000)
    box.fill("平安银行一年前买入10000股，盈利20%全部卖出")
    box.press("Enter")
    page.get_by_role("button", name="查看并修改").wait_for(timeout=180_000)
    page.get_by_role("button", name="查看并修改").click()
    page.get_by_text("建仓：区间首个交易日开盘提交买入10000股", exact=False).wait_for(
        timeout=30_000
    )
    page.get_by_role("button", name="开始回测").click()
    page.get_by_text("每笔委托", exact=True).wait_for(timeout=180_000)
    report_text = page.locator("body").inner_text()
    (out / "report.txt").write_text(report_text)
    page.screenshot(path=str(out / "report.png"), full_page=True)
    page.get_by_role("tab", name="工作流").click()
    workflow = page.locator("#detail-panel-flow")
    workflow.get_by_text("卖出条件：较持仓成交均价上涨20%", exact=False).first.wait_for()
    workflow_text = workflow.inner_text()
    assert "阶段1：" not in workflow_text
    (out / "workflow.txt").write_text(workflow_text)
    page.screenshot(path=str(out / "workflow.png"), full_page=True)
    workflow.locator("button").filter(has_text="成交规则与数据依据").click()
    capacity = page.get_by_text(
        "首笔开盘委托按首根完整分钟成交量，后续按上一已完成分钟成交量 × 5%",
        exact=True,
    )
    capacity.wait_for(timeout=30_000)
    text = page.locator("body").inner_text()
    assert "未设置买入规则" not in text
    page.screenshot(path=str(out / "execution-details.png"), full_page=True)
    (out / "execution-details.txt").write_text(text)
    (out / "network.json").write_text(
        json.dumps(responses, ensure_ascii=False, indent=2)
    )
    print(
        json.dumps(
            {
                "url": page.url,
                "openingPurchaseVisible": True,
                "singleRuleHasNoStageNumber": True,
                "capacityAssumptionVisible": True,
                "apiFailures": [item for item in responses if item["http"] >= 400],
            },
            ensure_ascii=False,
        )
    )
    browser.close()
