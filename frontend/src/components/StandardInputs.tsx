import { AdapterFields } from "./AdapterFields";
import { useEffect, useState } from "react";
import { apiGet, apiGetOptions, apiGetPage, apiPost } from "../services/api";
import { ManagementDrawer } from "./ManagementDrawer";
import { OffsetPagination } from "./OffsetPagination";

type TagCatalog = { status: string; tags: { name: string; revision: string }[] };
function useRepositoryTags() {
  const [catalog, setCatalog] = useState<TagCatalog>({ status: "loading", tags: [] });
  useEffect(() => {
    const controller = new AbortController();
    apiGet<TagCatalog>("/standard-inputs/tags", controller.signal).then((value) => {
      if (!controller.signal.aborted) setCatalog(value && Array.isArray(value.tags) ? value : { status: "unavailable", tags: [] });
    }).catch(() => { if (!controller.signal.aborted) setCatalog({ status: "unavailable", tags: [] }); });
    return () => controller.abort();
  }, []);
  return catalog;
}
function tagLabel(catalog: TagCatalog, revision: string) {
  const matched = catalog.tags.filter((tag) => tag.revision === revision).map((tag) => tag.name);
  return matched.length ? matched.join("、") : catalog.status === "available" ? "无对应 Tag" : catalog.status === "loading" ? "Tag 查询中…" : "Tag 信息暂不可用";
}


const missingLabel = (reason: string) => ({
  model_mapping_missing: "模型映射", target_missing: "模型服务绑定",
  tool_root_missing: "AISBench 源码目录", datasets_dir_missing: "数据集目录",
  task_path_missing: "任务目录或数据集版本", agent_missing: "Agent 参数",
  startup_template_required: "启动模板",
}[reason] || reason);

type Bundle = { repository: string; revision: string; files: Record<string, string> };
type InputVersion = {
  id: string; source_model: string; benchmark: string; revision: string; sha256: string;
  startup_missing: boolean; locked_fields: Record<string, unknown>;
  field_changes?: { field: string; before: unknown; after: unknown; change: string }[];
  mapping: { dataset: string; variant: string }; changed_files?: string[]; removed_files?: string[];
  files: Record<string, { path: string; raw: string; sha256: string }>;
  latest_compilation?: { input_ready: boolean; template_version: string; blocking_reasons?: string[] } | null;
  compilations?: Compilation[];
};
type Preview = { bundle: Bundle; sha256: string; entries: InputVersion[] };
type Compilation = { input_ready: boolean; blocking_reasons: string[]; sha256: string; config: unknown; locked_fields: Record<string, unknown>; field_sources: Record<string, string>; overrides: unknown[]; template_version: string; model_id: string | null };
type Model = { id: string; model_id: string; name: string };
type Target = { id: string; model_id: string; remote_model: string; base_url: string };

function CompileForm({ entry }: { entry: InputVersion }) {
  const [models, setModels] = useState<Model[]>([]);
  const [targets, setTargets] = useState<Target[]>([]);
  const [modelId, setModelId] = useState("");
  const [targetId, setTargetId] = useState("");
  const [template, setTemplate] = useState(JSON.stringify({ tool: "ais_bench", subcommand: entry.benchmark === "DeepSWE" ? "harbor_agent" : "llm_accuracy", params: {} }, null, 2));
  const [adjustments, setAdjustments] = useState(JSON.stringify({ tool: "ais_bench", subcommand: entry.benchmark === "DeepSWE" ? "harbor_agent" : "llm_accuracy", params: {} }, null, 2));
  const [version, setVersion] = useState("platform.v1");
  const [execution, setExecution] = useState("docker");
  const [datasetsDir, setDatasetsDir] = useState("");
  const [taskPath, setTaskPath] = useState("");
  const [toolRoot, setToolRoot] = useState("");
  const [result, setResult] = useState<Compilation | null>(null);
  const [suggestion, setSuggestion] = useState<{ preview: Compilation; template_source: string; notes: string[]; changes: unknown } | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    Promise.all([apiGetOptions<Model[]>("/models", controller.signal), apiGetOptions<Target[]>("/target-bindings", controller.signal)])
      .then(([m, t]) => { if (!controller.signal.aborted) { setModels(m.data); setTargets(t.data); } })
      .catch(() => { if (!controller.signal.aborted) setMessage("模型与服务选项加载失败，请关闭后重试。"); });
    return () => controller.abort();
  }, []);
  const changed = () => { setResult(null); setSuggestion(null); setMessage(""); };
  const autocomplete = async () => {
    setBusy(true); setMessage(""); setResult(null); setSuggestion(null);
    try {
      const response = await apiPost<{ candidate: { model_id: string | null; target_id: string | null; template: unknown; template_version: string }; preview: Compilation; template_source: string; notes: string[] }>(`/standard-inputs/${entry.id}/autocomplete`, {
        model_id: modelId || null, target_id: targetId || null, execution,
        adjustments: JSON.parse(adjustments), datasets_dir: datasetsDir || null,
        task_path: taskPath || null, tool_root: toolRoot || null,
      });
      setSuggestion({ ...response, changes: { before: JSON.parse(template), after: response.candidate.template } });
      setTemplate(JSON.stringify(response.candidate.template, null, 2));
      setVersion(response.candidate.template_version);
      setModelId(response.candidate.model_id || ""); setTargetId(response.candidate.target_id || "");
    } catch (error) { setMessage(error instanceof Error ? error.message : "自动补全失败"); }
    finally { setBusy(false); }
  };
  const compile = async () => {
    setBusy(true); setMessage(""); setResult(null);
    try {
      setResult(await apiPost<Compilation>(`/standard-inputs/${entry.id}/compile`, {
        model_id: modelId || null, target_id: targetId || null, template_version: version,
        template: JSON.parse(template), adjustments: JSON.parse(adjustments), execution,
        datasets_dir: datasetsDir || null, task_path: taskPath || null, tool_root: toolRoot || null,
      }));
    } catch (error) { setMessage(error instanceof Error ? error.message : "输入编译失败"); }
    finally { setBusy(false); }
  };
  return <div className="stack">
    <p className="notice">仅保存输入映射与编译快照，不启动测评、不批准配置。请勿粘贴密钥；后台通过已登记服务引用凭据。</p>
    <button disabled={busy} onClick={() => void autocomplete()}>自动补全配置</button>
    {suggestion && <section className="notice stack" aria-label="自动补全预览"><h3>候选配置已补全，待确认保存</h3><p>来源：{suggestion.template_source}</p>{suggestion.notes.map(note => <small key={note}>{note}</small>)}<p>{suggestion.preview.input_ready ? "输入字段齐备；环境仍需预检" : "仍需补全：" + suggestion.preview.blocking_reasons.join("、")}</p><details><summary>补全差异与字段来源</summary><pre className="snapshot">{JSON.stringify({ changes: suggestion.changes, field_sources: suggestion.preview.field_sources, overrides: suggestion.preview.overrides }, null, 2)}</pre></details></section>}
    <label>平台模型<select aria-label="平台模型" value={modelId} onChange={(e) => { setModelId(e.target.value); setTargetId(""); changed(); }}><option value="">请选择模型映射</option>{models.map((m) => <option key={m.id} value={m.id}>{m.model_id} · {m.name}</option>)}</select></label>
    <label>模型服务<select aria-label="模型服务" value={targetId} onChange={(e) => { setTargetId(e.target.value); changed(); }}><option value="">请选择已登记服务</option>{targets.filter((t) => t.model_id === modelId).map((t) => <option key={t.id} value={t.id}>{t.remote_model} · {t.base_url}</option>)}</select></label>
    <label>启动模板版本<input aria-label="启动模板版本" value={version} onChange={(e) => { setVersion(e.target.value); changed(); }} /></label>
    <label>启动模板 JSON<textarea aria-label="启动模板 JSON" rows={6} value={template} onChange={(e) => { setTemplate(e.target.value); changed(); }} /></label>
    <label>允许调整的参数 JSON<textarea rows={4} value={adjustments} onChange={(e) => { setAdjustments(e.target.value); changed(); }} /></label>
    <small>使用 tool/subcommand/params 结构；标准锁定参数最终覆盖，环境、端点及命令行绕过参数不可在此填写。模板内容修改需使用新版本。{entry.startup_missing ? "仓库缺少 benchmark_config.json，必须填写非空平台启动模板；允许调整的参数不能替代。" : ""}</small>
    <label>执行环境<select value={execution} onChange={(e) => { setExecution(e.target.value); changed(); }}><option value="docker">Docker</option><option value="local">执行服务器本机</option></select></label>
    <label>AISBench 源码目录<input aria-label="AISBench 源码目录" value={toolRoot} onChange={(e) => { setToolRoot(e.target.value); changed(); }} placeholder="已批准执行环境中的绝对路径" /></label>
    {entry.benchmark !== "DeepSWE" ? <label>数据集目录<input aria-label="数据集目录" value={datasetsDir} onChange={(e) => { setDatasetsDir(e.target.value); changed(); }} /></label> : <label>任务目录<input value={taskPath} onChange={(e) => { setTaskPath(e.target.value); changed(); }} placeholder="需与标准锁定路径一致" /></label>}
    <button className="primary" disabled={busy} onClick={() => void compile()}>{busy ? "校验中…" : "校验并保存输入快照"}</button>
    {message && <p role="alert">{message}</p>}
    {result && <section className="stack" aria-label="输入校验结果"><h3>{result.input_ready ? "输入字段校验通过" : "输入待补全"}</h3><p>执行未开放 · 配置未批准 · 环境与工具能力尚未验证</p>{result.blocking_reasons.length > 0 && <ul>{result.blocking_reasons.map((reason) => <li key={reason}>{missingLabel(reason)}</li>)}</ul>}<small>配置 SHA-256：{result.sha256}</small><details open><summary>脱敏启动配置</summary><pre className="snapshot">{JSON.stringify(result.config, null, 2)}</pre></details><details><summary>锁定字段、来源及覆盖差异</summary><pre className="snapshot">{JSON.stringify({ locked_fields: result.locked_fields, field_sources: result.field_sources, overrides: result.overrides }, null, 2)}</pre></details></section>}
  </div>;
}

function InputDetail({ entry }: { entry: InputVersion }) {
  const tags = useRepositoryTags();
  const [detail, setDetail] = useState(entry);
  const [message, setMessage] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    apiGet<InputVersion>(`/standard-inputs/${entry.id}`, controller.signal).then((row) => { if (!controller.signal.aborted) setDetail(row); }).catch(() => { if (!controller.signal.aborted) setMessage("历史编译记录加载失败"); });
    return () => controller.abort();
  }, [entry.id]);
  const config = detail.files["standard_config.json"] ? JSON.parse(detail.files["standard_config.json"].raw) : {};
  return <div className="stack standard-detail"><div className="standard-detail-meta"><strong>{detail.source_model}</strong><span>版本：{tagLabel(tags, detail.revision)}</span><span>未批准 · 未开放执行</span></div>
    <div className="workflow-grid"><aside className="workflow-datasets"><h3>测评基准</h3><div className="notice">{detail.benchmark}</div><p className="muted">标准参数只读</p></aside><section className="card stack"><h3>{detail.benchmark}</h3><AdapterFields value={config} readonly /></section></div>
    <details><summary>来源与校验信息</summary><p>提交：{detail.revision}</p><p>内容 SHA-256：{detail.sha256}</p><p>平台映射：{detail.mapping.dataset} / {detail.mapping.variant}</p></details>
    {Object.entries(detail.files).map(([name, file]) => <details key={name}><summary>{name}{name === "baseline.json" ? "（来源附件，未批准为基线）" : ""}</summary><a href={`https://github.com/SJTUyh/AutoEval-Standard/blob/${detail.revision}/${file.path}`} target="_blank" rel="noreferrer">查看来源文件</a><small>SHA-256：{file.sha256}</small><pre className="snapshot">{file.raw}</pre></details>)}
    <ManagementDrawer title="补全启动参数与模型映射"><CompileForm entry={detail} /></ManagementDrawer>
    {message && <p role="alert">{message}</p>}
    <details><summary>历史编译快照（最近 20 条）</summary>{detail.compilations?.length ? detail.compilations.map((item) => <div key={item.sha256}><p>{item.template_version} · {item.input_ready ? "输入齐备" : "待补全"} · {item.model_id || "未映射模型"}</p><pre className="snapshot">{JSON.stringify(item, null, 2)}</pre></div>) : <p>暂无编译记录；保存后重新打开详情可查看。</p>}</details>
  </div>;
}

export function StandardInputs() {
  const tags = useRepositoryTags();
  const [rows, setRows] = useState<InputVersion[]>([]);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [ref, setRef] = useState("main");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [offset, setOffset] = useState(0);
  const [next, setNext] = useState<number | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    apiGetPage<InputVersion[]>(`/standard-inputs?offset=${offset}&limit=20`, controller.signal).then((page) => { if (!controller.signal.aborted) { setRows(page.data); setNext(page.next_offset); } }).catch(() => { if (!controller.signal.aborted) setMessage("标准输入读取失败，请检查数据库迁移后刷新。"); });
    return () => controller.abort();
  }, [offset, revision]);
  const perform = async (fn: () => Promise<void>) => { if (busy) return; setBusy(true); setMessage(""); try { await fn(); } catch (error) { setMessage(error instanceof Error ? error.message : "请求失败"); } finally { setBusy(false); } };
  return <section className="configuration-management stack"><div className="spread"><h3>标准仓输入</h3><button disabled={busy} onClick={() => setRevision((v) => v + 1)}>刷新输入版本</button></div><p className="muted">同步标准与来源附件，保留不可变版本；导入不等于配置审批，也不开放测评执行。</p>
    <div className="inline"><label>分支、Tag 或提交<input aria-label="标准仓分支或提交" value={ref} onChange={(e) => { setRef(e.target.value); setPreview(null); }} /></label><button disabled={busy} onClick={() => void perform(async () => { setPreview(null); setPreview(await apiPost<Preview>("/standard-inputs/sync-preview", { ref })); })}>同步预览</button><label>离线输入包<input className="standard-package-upload" aria-label="选择离线输入包" type="file" accept=".json,application/json" disabled={busy} onChange={(e) => { const file = e.target.files?.[0]; if (!file) return; setPreview(null); void perform(async () => { if (file.size > 3_000_000) throw Error("输入包不能超过 3 MB"); setPreview(await apiPost<Preview>("/standard-inputs/preview", JSON.parse(await file.text()))); }); e.target.value = ""; }} /></label></div>
    <small>离线目录先使用后台 import_standard_inputs 工具生成输入包。远程分支会先固定为完整提交号。</small>
    {message && <p role="status">{message}</p>}
    {preview && <section className="notice stack"><h3>导入预览</h3><p>版本 Tag：{tagLabel(tags, preview.bundle.revision)}</p><p>提交：{preview.bundle.revision}</p><ul>{preview.entries.map((entry) => <li key={`${entry.source_model}/${entry.benchmark}`}>{entry.source_model} / {entry.benchmark} · {entry.startup_missing ? "待补全启动配置" : "包含启动配置"}<small>变更：{entry.changed_files?.join("、") || "无"}；移除：{entry.removed_files?.join("、") || "无"}</small><details><summary>查看标准参数差异与原文</summary><pre className="snapshot">{JSON.stringify({ field_changes: entry.field_changes, locked_fields: entry.locked_fields, files: entry.files }, null, 2)}</pre></details></li>)}</ul><button disabled={busy} className="primary" onClick={() => void perform(async () => { await apiPost("/standard-inputs/import", { bundle: preview.bundle, expected_sha256: preview.sha256 }); setPreview(null); setRevision((v) => v + 1); setMessage("标准输入已登记，配置未批准，执行未开放。"); })}>确认登记输入版本</button></section>}
    <div className="table-wrap"><table><thead><tr><th>来源模型</th><th>基准 / 变体</th><th>版本 / 提交</th><th>状态</th><th>操作</th></tr></thead><tbody>{rows.map((row) => <tr key={row.id}><td>{row.source_model}</td><td>{row.benchmark}<small>平台内部映射：{row.mapping.dataset} / {row.mapping.variant}</small></td><td title={row.revision}>{tagLabel(tags, row.revision)}<small>{row.revision.slice(0, 12)}</small></td><td>{row.latest_compilation ? row.latest_compilation.input_ready ? "输入字段齐备，待环境预检" : "已编译，仍有缺失项" : row.startup_missing ? "待补全启动配置" : "已登记，待校验"}<small>{row.latest_compilation?.blocking_reasons?.length ? "缺少：" + row.latest_compilation.blocking_reasons.map(missingLabel).join("、") : ""}</small><small>未批准 · 执行未开放</small></td><td><ManagementDrawer title="查看输入详情"><InputDetail entry={row} /></ManagementDrawer></td></tr>)}</tbody></table></div>{!rows.length && <p className="empty">暂无标准仓输入版本</p>}<OffsetPagination label="标准输入" offset={offset} nextOffset={next} onChange={setOffset} disabled={busy} />
  </section>;
}
