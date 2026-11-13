"""Unified output presentation uses intercepted data, never starts a benchmark."""
from pathlib import Path

from playwright.sync_api import expect


def test_unified_token_coverage_and_unknown_values(browser_page):
    page, url, _ = browser_page
    page.add_init_script(path=Path(__file__).with_name("charts.js"))
    page.add_init_script("""(() => {
      const original=window.fetch;
      window.fetch=async (url, options)=>String(url).includes('/unified-output')
        ? new Response(JSON.stringify({data:{validity:'partial',observed_cases:2,expected_cases:3,missing_cases:1,
            token_coverage:{known_attempts:1,total_attempts:2,input_tokens_known_sum:null,output_tokens_known_sum:0}}}),
            {headers:{'Content-Type':'application/json'}})
        : original(url, options);
    })()""")
    page.goto(url + "/#report/chart-fixture")
    expect(page.get_by_role("heading", name="统一输出覆盖情况")).to_have_count(0)
    page.get_by_role("button", name="查看结果", exact=True).click()
    page.get_by_role("button", name="样本明细", exact=True).click()
    summary = page.locator(".unified-summary")
    expect(summary).to_contain_text("覆盖 2 / 3 个 case")
    expect(summary).to_contain_text("缺测 1")
    expect(summary).to_contain_text("输入 — / 输出 0")
