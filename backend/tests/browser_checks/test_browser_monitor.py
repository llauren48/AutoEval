from pathlib import Path

from playwright.sync_api import expect

FIXTURE = Path(__file__).with_name("monitor.js")


def open_monitor(page, url):
    page.add_init_script(path=FIXTURE)
    page.goto(url + "/#run/fixture-run")
    page.get_by_role("button", name="监控", exact=True).click()


def test_monitor_loading_is_not_reported_as_not_collected(browser_page):
    page, url, _ = browser_page
    page.add_init_script(path=FIXTURE)
    page.add_init_script(
        "window.monitorDelay=500; window.monitor={collection_status:'not_collected',stale:false}"
    )
    page.goto(url + "/#run/fixture-run")
    page.get_by_role("button", name="监控", exact=True).click()
    expect(page.get_by_text("正在读取工具监控…", exact=True)).to_be_visible()
    expect(page.get_by_text("本次运行未采集工具监控。", exact=True)).to_have_count(0)
    expect(page.get_by_text("本次运行未采集工具监控。", exact=True)).to_be_visible()


def test_monitor_polling_visibility_stale_terminal_and_controlled_evidence(browser_page):
    page, url, _ = browser_page
    open_monitor(page, url)
    expect(page.get_by_role("heading", name="工具运行监控", exact=True)).to_be_visible()
    expect(page.get_by_text("实时观测", exact=True)).to_be_visible()
    page.wait_for_function("window.monitorCalls >= 3")

    page.evaluate("window.fixtureHidden=true")
    hidden_calls = page.evaluate("window.monitorCalls")
    page.wait_for_timeout(2200)
    assert page.evaluate("window.monitorCalls") == hidden_calls
    page.evaluate("window.fixtureHidden=false")
    page.wait_for_function(f"window.monitorCalls > {hidden_calls}")

    page.evaluate("window.monitor.stale=true")
    expect(page.get_by_text("数据已过期 / 不可达", exact=False)).to_be_visible()
    expect(page.get_by_role("progressbar", name="工具执行进度")).to_have_attribute("aria-valuenow", "40")

    page.evaluate(
        "window.detail.state='completed'; window.monitor.stale=false; window.monitor.snapshot_kind='final'; window.monitor.tool_status='finish'; window.monitor.finish_count=5"
    )
    expect(page.get_by_text("终态已归档", exact=True)).to_be_visible()
    terminal_calls = page.evaluate("window.monitorCalls")
    page.wait_for_timeout(2200)
    assert page.evaluate("window.monitorCalls") == terminal_calls

    page.get_by_role("button", name="归档快照", exact=True).click()
    page.get_by_role("button", name="final", exact=False).click()
    expect(page.get_by_test_id("monitor-evidence")).to_contain_text('"snapshot_kind": "final"')

    page.get_by_role("button", name="日志", exact=True).click()
    expect(page.get_by_test_id("monitor-log")).to_contain_text("fixture archived log")
    page.evaluate("window.logFailure=true")
    page.get_by_role("button", name="概览", exact=True).click()
    page.get_by_role("button", name="日志", exact=True).click()
    expect(page.get_by_text("本次运行未归档执行日志，或日志校验未通过。", exact=True)).to_be_visible()


def test_monitor_last_observed_is_not_called_final(browser_page):
    page, url, _ = browser_page
    page.add_init_script(path=FIXTURE)
    page.add_init_script(
        "window.detail.state='completed'; window.monitor.snapshot_kind='last_observed'"
    )
    page.goto(url + "/#run/fixture-run")
    page.get_by_role("button", name="监控", exact=True).click()
    expect(page.get_by_text("仅最后观测（非终态）", exact=True)).to_be_visible()
    expect(page.get_by_text("终态已归档", exact=True)).to_have_count(0)


def test_case_detail_uses_server_state_filter_and_row_evidence(browser_page):
    page, url, _ = browser_page
    page.add_init_script(path=FIXTURE)
    page.goto(url + "/#run/fixture-run")
    expect(page.get_by_text("判分准确率", exact=True)).to_be_visible()
    expect(page.get_by_text("80%", exact=True)).to_be_visible()
    expect(page.get_by_test_id("run-progress")).to_be_visible()
    expect(page.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "40")

    table = page.get_by_test_id("case-attempts")
    expect(table).to_contain_text("UPSTREAM_TIMEOUT")
    expect(table).to_contain_text("执行成功")

    page.get_by_label("Case执行状态筛选").select_option("failed")
    expect(table).to_contain_text("UPSTREAM_TIMEOUT")
    expect(table).not_to_contain_text("执行成功")
    assert any("state=failed" in query for query in page.evaluate("window.caseQueries"))

    page.get_by_role("button", name="查看第1次证据", exact=True).click()
    dialog = page.locator("dialog")
    expect(dialog).to_contain_text("Fixture prompt")
    expect(dialog).to_contain_text("UPSTREAM_TIMEOUT")
    expect(page.get_by_test_id("case-evidence")).to_contain_text('"state": "failed"')


def test_monitor_progress_unknown_zero_and_completed(browser_page):
    page, url, _ = browser_page
    page.add_init_script(path=FIXTURE)
    page.add_init_script("window.monitor.finish_count=null; window.detail.state='completed'")
    page.goto(url + "/#run/fixture-run")
    bar = page.get_by_role("progressbar", name="工具执行进度")
    expect(page.get_by_text("未记录样本进度", exact=True)).to_be_visible()
    assert bar.get_attribute("aria-valuenow") is None
    page.evaluate("window.monitor.finish_count=0")
    page.get_by_role("button", name="监控", exact=True).click()
    expect(bar).to_have_attribute("aria-valuenow", "0")
    expect(page.get_by_text("0 / 5 · 0%", exact=True)).to_be_visible()
    page.evaluate("window.monitor.finish_count=5;window.monitor.snapshot_kind='final'")
    page.get_by_role("button", name="执行进度", exact=True).click()
    expect(bar).to_have_attribute("aria-valuenow", "100")
    expect(page.get_by_text("5 / 5 · 100%", exact=True)).to_be_visible()


def test_case_list_refreshes_when_running_task_completes(browser_page):
    page, url, _ = browser_page
    page.add_init_script(path=FIXTURE)
    page.add_init_script("window.savedCases=window.cases;window.cases=[];window.detail.report=null")
    page.goto(url + "/#run/fixture-run")
    expect(page.get_by_text("该运行暂无 case 执行记录。", exact=True)).to_be_visible()
    page.evaluate("window.cases=window.savedCases;window.detail.state='completed'")
    expect(page.get_by_test_id("case-attempts")).to_be_visible(timeout=10000)
    expect(page.get_by_test_id("case-attempts")).to_contain_text("UPSTREAM_TIMEOUT")


def test_waiting_progress_has_phases_live_elapsed_and_terminal_freeze(browser_page):
    from datetime import UTC, datetime
    page, url, _ = browser_page
    page.clock.install(time=datetime(2026, 11, 12, 12, 0, tzinfo=UTC))
    page.add_init_script(path=FIXTURE)
    page.add_init_script("window.detail.state='queued';window.detail.created_at='2026-11-12T11:58:55Z';window.monitor={collection_status:'not_collected',stale:false}")
    page.goto(url + "/#run/fixture-run")
    panel = page.get_by_role("region", name="任务进度与耗时")
    expect(panel.get_by_text("提交与排队", exact=True)).to_be_visible()
    expect(panel.locator('[aria-current="step"]')).to_contain_text("提交与排队")
    expect(page.get_by_test_id("run-elapsed")).to_have_text("1分 05秒")
    assert page.get_by_role("progressbar").get_attribute("aria-valuenow") is None
    page.clock.run_for(2000)
    expect(page.get_by_test_id("run-elapsed")).to_have_text("1分 07秒")
    page.evaluate("window.detail.state='completed';window.detail.events=[{seq:2,type:'completed',created_at:'2026-11-12T12:00:02Z'}]")
    page.clock.run_for(3000)
    expect(panel.get_by_text("总耗时", exact=True)).to_be_visible()
    expect(page.get_by_test_id("run-elapsed")).to_have_text("1分 07秒")
    page.clock.run_for(5000)
    expect(page.get_by_test_id("run-elapsed")).to_have_text("1分 07秒")


def test_unknown_progress_is_empty_and_stage_animates_only_while_active(browser_page):
    page, url, _ = browser_page
    page.add_init_script(path=FIXTURE)
    page.add_init_script("window.detail.state='preparing';window.detail.events=[{type:'preparing',created_at:window.detail.created_at}];window.monitor.finish_count=null")
    page.goto(url + "/#run/fixture-run")
    panel = page.get_by_role("region", name="任务进度与耗时")
    expect(panel.locator(".phase-spinner")).to_have_count(1)
    expect(panel.get_by_text("等待样本计数", exact=True)).to_be_visible()
    bar = panel.get_by_role("progressbar")
    assert bar.get_attribute("aria-valuenow") is None
    assert bar.locator("i").evaluate("e => getComputedStyle(e).width") == "0px"
    page.evaluate("window.detail.state='failed'")
    expect(panel.locator(".phase-spinner")).to_have_count(0)
    expect(panel.get_by_text("未记录样本进度", exact=True)).to_be_visible()
    expect(panel.locator('[aria-current="step"]')).to_contain_text("环境准备")
