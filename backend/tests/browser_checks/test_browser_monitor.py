from pathlib import Path

from playwright.sync_api import expect

FIXTURE = Path(__file__).with_name("monitor.js")


def open_monitor(page, url):
    page.add_init_script(path=FIXTURE)
    page.goto(url + "/#run/fixture-run")


def test_monitor_loading_is_not_reported_as_not_collected(browser_page):
    page, url, _ = browser_page
    page.add_init_script(path=FIXTURE)
    page.add_init_script(
        "window.monitorDelay=500; window.monitor={collection_status:'not_collected',stale:false}"
    )
    page.goto(url + "/#run/fixture-run")
    expect(page.get_by_text("正在读取工具监控…", exact=True)).to_be_visible()
    expect(page.get_by_text("本次运行未采集工具监控。", exact=True)).to_have_count(0)
    expect(page.get_by_text("本次运行未采集工具监控。", exact=True)).to_be_visible()


def test_monitor_polling_visibility_stale_terminal_and_controlled_evidence(browser_page):
    page, url, _ = browser_page
    open_monitor(page, url)
    expect(page.get_by_role("heading", name="工具运行监控", exact=True)).to_be_visible()
    expect(page.get_by_text("远程 AISBench", exact=True).first).to_be_visible()
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
    expect(page.get_by_text("进度：2 / 5", exact=False)).to_be_visible()

    page.evaluate(
        "window.detail.state='completed'; window.monitor.stale=false; window.monitor.snapshot_kind='final'; window.monitor.tool_status='finish'; window.monitor.finish_count=5"
    )
    expect(page.get_by_text("终态已归档", exact=True)).to_be_visible()
    terminal_calls = page.evaluate("window.monitorCalls")
    page.wait_for_timeout(2200)
    assert page.evaluate("window.monitorCalls") == terminal_calls

    page.get_by_role("button", name="监控明细", exact=True).click()
    page.get_by_role("button", name="查看归档快照", exact=True).click()
    page.get_by_role("button", name="final ·", exact=False).click()
    expect(page.locator("dialog")).to_contain_text('"snapshot_kind": "final"')
    page.get_by_role("button", name="查看归档日志", exact=True).click()
    expect(page.locator("dialog")).to_contain_text("fixture archived log")
    page.evaluate("window.logFailure=true")
    page.get_by_role("button", name="查看归档日志", exact=True).click()
    expect(page.locator("dialog")).to_contain_text("证据尚未归档或校验未通过")


def test_monitor_last_observed_is_not_called_final(browser_page):
    page, url, _ = browser_page
    page.add_init_script(path=FIXTURE)
    page.add_init_script(
        "window.detail.state='completed'; window.monitor.snapshot_kind='last_observed'"
    )
    page.goto(url + "/#run/fixture-run")
    expect(page.get_by_text("仅最后观测（非终态）", exact=True)).to_be_visible()
    expect(page.get_by_text("终态已归档", exact=True)).to_have_count(0)
