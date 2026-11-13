"""UI contract checks; all API responses are simulated, never real model requests."""

from pathlib import Path

from playwright.sync_api import expect


def test_preflight_blocks_requires_confirmation_and_invalidates(browser_page):
    page, url, _ = browser_page
    page.add_init_script(path=Path(__file__).with_name("preflight.js"))
    page.goto(url + "/#console")
    def choose(label, value):
        select = page.get_by_label(label, exact=True)
        select.locator("xpath=ancestor::details[1]").locator("summary").click()
        select.select_option(value)
    choose("控制台模型服务", "target-1")
    choose("控制台执行资源", "resource-1")
    check = page.get_by_role("button", name="检查服务、资源与配置", exact=True)
    submit = page.get_by_role("button", name="发起测评", exact=True)
    check.click()
    expect(page.get_by_text("资源未就绪", exact=False)).to_be_visible()
    expect(submit).to_be_disabled()
    page.evaluate("window.hostReady = true")
    check.click()
    expect(page.get_by_text("容器就绪", exact=False)).to_be_visible()
    expect(submit).to_be_disabled()
    page.locator(".submission-review input[type=checkbox]").check()
    expect(submit).to_be_enabled()
    choose("控制台执行资源", "resource-2")
    expect(submit).to_be_disabled()
    assert page.evaluate("window.generated") == 0


def test_report_charts_comparison_and_missing_values(browser_page):
    page, url, _ = browser_page
    page.add_init_script(path=Path(__file__).with_name("charts.js"))
    page.goto(url + "/#report/chart-fixture")
    expect(page.locator(".report-charts")).to_contain_text("+5.00 个百分点")
    meters = page.locator(".report-charts [role=meter]")
    expect(meters).to_have_count(2)

    def change(code, name):
        # A new route forces a fresh report request without arbitrary sleeps.
        page.evaluate(code + f'; location.hash="report/{name}"')
        expect(page.locator(".breadcrumb")).to_contain_text("测评报告")

    change("window.chartReport.summary.accuracy=0;"
           "window.chartReport.baseline_comparison.measured_value=0;"
           "window.chartReport.baseline_comparison.baseline_value=0", "zero")
    expect(page.locator('.report-charts [aria-valuenow="0"]')).to_have_count(2)
    change('window.chartReport.baseline_comparison.status="missing"', "missing")
    expect(meters).to_have_count(1)
    change('window.chartReport.baseline_comparison.status="matched";'
           'window.chartReport.validity="partial"', "partial")
    expect(page.locator(".accuracy-comparison")).to_contain_text("部分覆盖")
    expect(meters).to_have_count(1)
    change('window.chartReport.evidence_status="missing"', "unverified")
    expect(meters).to_have_count(0)
    expect(page.locator(".performance-chart-empty")).to_contain_text("暂无性能测评数据")


def test_analysis_start_uses_independent_start_contract(browser_page):
    page, url, _ = browser_page
    page.add_init_script(path=Path(__file__).with_name("analysis.js"))
    page.goto(url + "/#report/analysis-fixture")
    page.get_by_role("tab", name="智能分析").click()
    page.get_by_role("button", name="启动独立分析", exact=True).click()
    expect(page.get_by_text("状态：排队中", exact=False)).to_be_visible()
    calls = page.evaluate("window.analysisCalls")
    assert len(calls) == 1
    assert calls[0]["href"].endswith("/reports/analysis-fixture/analysis:start")
    assert calls[0]["method"] == "POST"


def test_analysis_catalog_labels_cover_demo_states(browser_page):
    page, url, _ = browser_page
    page.add_init_script(path=Path(__file__).with_name("analysis.js"))
    page.goto(url + "/#reports")
    labels = {
        "queued": "排队中",
        "not_enabled": "未启用",
        "not_queued": "待排队",
        "unresolved": "待查证",
        "running": "分析中",
        "succeeded": "分析成功",
        "failed": "分析失败",
    }
    for state, label in labels.items():
        page.evaluate(
            "value => sessionStorage.setItem('analysisState', value)",
            state,
        )
        page.reload()
        expect(page.get_by_test_id("real-report-catalog")).to_be_visible()
        expect(page.locator(".reports-table tbody tr .badge")).to_have_text(label)


def test_non_real_report_does_not_offer_analysis_start(browser_page):
    page, url, _ = browser_page
    page.add_init_script(path=Path(__file__).with_name("analysis.js"))
    page.goto(url + "/#reports")
    page.evaluate(
        "sessionStorage.setItem('analysisOrigin', 'simulation');"
        "sessionStorage.setItem('analysisState', 'not_queued')"
    )
    page.goto(url + "/#report/analysis-fixture")
    page.get_by_role("tab", name="智能分析").click()
    expect(page.get_by_text("非真实执行来源，不启用智能分析", exact=False)).to_be_visible()
    expect(page.get_by_role("button", name="启动独立分析", exact=True)).to_have_count(0)
    assert page.evaluate("window.analysisCalls") == []
