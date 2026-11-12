"""Explicit opt-in real fixture submission: no benchmark or paid generation."""

import os

import pytest
from playwright.sync_api import expect


@pytest.mark.skipif(
    os.getenv("AE_FIXTURE_PIPELINE_TEST") != "1", reason="Explicit fixture submission opt-in"
)
def test_real_fixture_frontend_submission_and_report(browser_page):
    page, url, artifacts = browser_page
    # Only this guarded, server-side fixture endpoint may write. All other writes remain blocked.
    page.context.route(
        "**/api/autoeval/v1/diagnostics/unified-fixture", lambda route: route.continue_()
    )
    page.goto(url + "/#data-workbench")
    page.get_by_role("button", name="环境诊断", exact=True).click()
    select = page.get_by_label("流程验证标准配置", exact=True)
    expect(select.locator("option")).not_to_have_count(1)
    value = select.locator("option").nth(1).get_attribute("value")
    select.select_option(value)
    with page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/diagnostics/unified-fixture")
    ) as response:
        page.get_by_role("button", name="启动无模型流程验证", exact=True).click()
    assert response.value.status == 202
    run_id = response.value.json()["data"]["run_id"]
    page.get_by_role("link", name="查看流程验证任务").click()
    expect(page.get_by_role("heading", name="任务总览", exact=True)).to_be_visible()
    import time

    report_id = None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = page.request.get(url + f"/api/autoeval/v1/runs/{run_id}")
        assert result.ok, result.text()
        data = result.json()["data"]
        report = data.get("report")
        if report:
            report_id = report["id"]
            break
        page.wait_for_timeout(500)
    assert report_id, "Fixture workflow did not produce report within 60 seconds"
    page.goto(url + "/#report/" + report_id)
    expect(
        page.get_by_text(
            "人工测试产物流程验证报告；不代表真实模型成绩，不参与榜单或正式发布门禁。", exact=True
        )
    ).to_be_visible()
    expect(page.get_by_role("heading", name="统一输出覆盖情况")).to_be_visible()
    page.screenshot(path=str(artifacts / "fixture-report-e2e.png"), full_page=True)
    (artifacts / "fixture-e2e-identifiers.txt").write_text(
        f"run_id={run_id}\nreport_id={report_id}\nworkflow_id=demo/run/{run_id}\n", encoding="utf-8"
    )
