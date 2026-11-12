"""Simulated input management only: all API writes intercepted before networking."""

import json
from pathlib import Path

from playwright.sync_api import expect


def test_standard_preview_import_mapping_and_compilation_drawers(browser_page):
    page, url, _ = browser_page
    source = json.loads(
        (Path(__file__).parents[1] / "fixtures/standard_inputs/upstream.json").read_text(
            encoding="utf-8"
        )
    )
    page.add_init_script("window.standardBundle=" + json.dumps(source))
    page.add_init_script("""(() => {
      window.inputImported = false; window.compiledInput = null; window.forbiddenCalls = [];
      const row = {id:'input-fixture',source_model:'fixture model',benchmark:'GPQA_Diamond',
        revision:window.standardBundle.revision,sha256:'a'.repeat(64),startup_missing:true,
        mapping:{dataset:'GPQA',variant:'diamond'},locked_fields:{'params.max_out_len':262144},
        files:{'standard_config.json':{path:'fixture/GPQA_Diamond/standard_config.json',raw:'{}',sha256:'b'.repeat(64)}},
        changed_files:['standard_config.json'],field_changes:[{field:'params.max_out_len',before:64,after:262144,change:'modified'}]};
      window.fetch=async (input, init={})=>{
        const p=String(input); let data=[];
        if(p.includes('/runs') && init.method==='POST') {window.forbiddenCalls.push(p); throw Error('forbidden');}
        if(p.includes('/standard-inputs/sync-preview') || p.includes('/standard-inputs/preview'))
          data={bundle:window.standardBundle,sha256:'c'.repeat(64),entries:[row]};
        else if(p.includes('/standard-inputs/import')) {window.inputImported=true;data=[row];}
        else if(p.includes('/standard-inputs/input-fixture/autocomplete')) {
          data={candidate:{model_id:'model-1',target_id:'target-1',template_version:'auto.v1.fixture',template:{tool:'ais_bench',subcommand:'llm_accuracy',params:{max_out_len:262144}}},preview:{input_ready:false,blocking_reasons:['tool_root_missing'],field_sources:{'params.max_out_len':'standard-lock'},overrides:[]},template_source:'platform-standard-template.v1:fixture',notes:['预览不保存、不批准、不执行']};
        } else if(p.includes('/standard-inputs/input-fixture/compile')) {
          window.compiledInput=JSON.parse(init.body);
          data={input_ready:true,blocking_reasons:[],sha256:'d'.repeat(64),config:{tool:'ais_bench',subcommand:'llm_accuracy',params:{max_out_len:262144,execution:'docker'}},locked_fields:row.locked_fields,field_sources:{},overrides:[]};
        } else if(p.includes('/standard-inputs/input-fixture')) data={...row,compilations:[]};
        else if(p.includes('/standard-inputs')) data=window.inputImported?[row]:[];
        else if(p.includes('/system/capabilities')) data={evaluation_enabled:false};
        else if(p.includes('/models')) data=[{id:'model-1',model_id:'test/model',name:'Fixture'}];
        else if(p.includes('/target-bindings')) data=[{id:'target-1',model_id:'model-1',remote_model:'fixture',base_url:'https://example.com/v1'}];
        return new Response(JSON.stringify({data,next_offset:null}),{status:200,headers:{'Content-Type':'application/json'}});
      };
    })()""")
    page.goto(url + "/#data-workbench")
    page.get_by_role("button", name="标准仓输入", exact=True).click()
    expect(page.get_by_text("暂无标准仓输入版本", exact=True)).to_be_visible()
    page.get_by_role("button", name="同步预览", exact=True).click()
    expect(page.get_by_role("heading", name="导入预览")).to_be_visible()
    page.get_by_text("查看标准参数差异与原文", exact=True).click()
    expect(page.locator("pre.snapshot")).to_contain_text('"before": 64')
    page.get_by_role("button", name="确认登记输入版本").click()
    expect(page.get_by_text("标准输入已登记，配置未批准，执行未开放。", exact=True)).to_be_visible()
    page.get_by_role("button", name="查看输入详情", exact=True).click()
    expect(page.get_by_role("dialog")).to_contain_text("标准参数只读")
    page.get_by_role("button", name="补全启动参数与模型映射").click()
    dialog = page.get_by_role("dialog", name="补全启动参数与模型映射", exact=True)
    expect(dialog).to_contain_text("必须填写非空平台启动模板")
    dialog.get_by_role("button", name="自动补全配置", exact=True).click()
    expect(dialog.get_by_role("region", name="自动补全预览")).to_contain_text("tool_root_missing")
    expect(dialog.get_by_label("启动模板版本", exact=True)).to_have_value("auto.v1.fixture")
    assert page.evaluate("window.compiledInput === null")
    dialog.get_by_label("平台模型", exact=True).select_option("model-1")
    dialog.get_by_label("模型服务", exact=True).select_option("target-1")
    dialog.get_by_label("启动模板 JSON", exact=True).fill('{"params":{"max_num_workers":1}}')
    dialog.get_by_label("AISBench 源码目录", exact=True).fill("/benchmark")
    dialog.get_by_label("数据集目录", exact=True).fill("/data")
    dialog.get_by_role("button", name="校验并保存输入快照").click()
    expect(dialog.get_by_role("heading", name="输入字段校验通过")).to_be_visible()
    expect(dialog).to_contain_text("执行未开放")
    dialog.get_by_label("启动模板版本", exact=True).fill("platform.v2")
    expect(dialog.get_by_role("heading", name="输入字段校验通过")).to_have_count(0)
    assert page.evaluate("window.compiledInput.model_id") == "model-1"
    assert page.evaluate("window.compiledInput.template.params.max_num_workers") == 1
    assert page.evaluate("window.forbiddenCalls.length") == 0
    assert "api_key" not in page.evaluate("JSON.stringify(window.compiledInput)")
    dialog.get_by_role("button", name="关闭补全启动参数与模型映射").click()
    page.get_by_role("button", name="关闭查看输入详情").click()
