import { datasetIdentity } from "../services/datasetIdentities";
import { WorkbenchDatasets } from "./WorkbenchDatasets";
import { StandardInputs } from "./StandardInputs";
import { FixturePipeline } from "./FixturePipeline";
import { ModelIdentifierSelect } from "./ModelIdentifierSelect";
import { VendorLogo } from "./VendorLogo";
import { WorkbenchModels } from "./WorkbenchModels";
import { useEffect, useState } from "react";
import { apiGet, apiGetPage, apiPost } from "../services/api";
import { DiagnosticTasks } from "./DiagnosticTasks";
import { ManagementDrawer } from "./ManagementDrawer";
import { OffsetPagination } from "./OffsetPagination";
import type { Report } from "../app/ReportView";

type Entry = {
  source_key: string; source_type: string; model_name: string; dataset: string; metric: string;
  value: number; unit: string; direction: string; scope: string; conditions: Record<string, unknown>;
  source_name: string; source_url: string | null; source_version: string; source_sha256: string;
  report_id: string | null; notes: string;
};
export type CuratedRecord = { id: string; state: string; revision: number; created_at: string; entry: Entry; rank?: number };
const displayScoreUnit = (unit: unknown) => String(unit).toLowerCase() === "points" ? "" : String(unit);
const displayMetadata = (value: unknown) => value === null || value === undefined || value === "" || value === "未披露" || value === "unknown" ? "-" : String(value);
const labels: Record<string, string> = { draft: "待评审", approved: "已入榜", rejected: "已驳回", withdrawn: "已撤回", report: "平台报告", official: "官方数据", manual: "待确认来源" };
const blank = () => ({ source_key: `manual-${crypto.randomUUID()}`, source_type: "official", model_name: "", dataset: "GPQA", metric: "accuracy", value: "", unit: "%", direction: "higher_is_better", scope: "full", conditions: "", source_name: "", source_url: "", source_version: "", source_sha256: "", report_id: null as string | null, notes: "" });

export function DataWorkbench() {
  const [tab, setTab] = useState("榜单数据维护");
  const [rows, setRows] = useState<CuratedRecord[]>([]);
  const [reports, setReports] = useState<Report[]>([]);
  const [offset, setOffset] = useState(0);
  const [next, setNext] = useState<number | null>(null);
  const [reportOffset, setReportOffset] = useState(0);
  const [reportNext, setReportNext] = useState<number | null>(null);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [draft, setDraft] = useState(blank);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<CuratedRecord | null>(null);
  const [reason, setReason] = useState("");
  const [importText, setImportText] = useState("");
  const [fileHash, setFileHash] = useState("");
  useEffect(() => {
    const controller = new AbortController(); setLoading(true);
    Promise.all([
      apiGetPage<CuratedRecord[]>(`/data-workbench/entries?offset=${offset}&limit=20`, controller.signal),
      apiGetPage<Report[]>(`/reports?offset=${reportOffset}&limit=20`, controller.signal),
    ]).then(([entries, catalog]) => { if (!controller.signal.aborted) { setRows(entries.data); setNext(entries.next_offset); setReports(catalog.data); setReportNext(catalog.next_offset); } })
      .catch(() => { if (!controller.signal.aborted) setMessage("数据读取失败，请检查服务及数据库迁移后刷新。旧数据可能过期。"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [offset, reportOffset, revision]);
  const perform = async (task: () => Promise<void>) => {
    if (busy) return; setBusy(true); setMessage("");
    try { await task(); setRevision((v) => v + 1); }
    catch (error) { setMessage(error instanceof Error ? error.message : "操作失败，请刷新查证后重试。"); }
    finally { setBusy(false); }
  };
  const save = () => perform(async () => {
    if (draft.source_type !== "official") throw Error("请选择官方来源，或从报告评审入榜选择已有平台报告；历史未分类记录须先确认来源。");
    if (!draft.value.trim() || !reason.trim()) throw Error("请填写成绩和录入 / 修改原因。");
    const entry = { ...draft, value: Number(draft.value), conditions: JSON.parse(draft.conditions), source_url: draft.source_url || null, notes: draft.notes + (editing ? "" : `\n录入说明：${reason}`) };
    if (editing) await apiPost(`/data-workbench/entries/${editing.id}/edit`, { entry, expected_revision: editing.revision, reason });
    else await apiPost("/data-workbench/entries", entry);
    setMessage("已保存待评审数据，尚未入榜。"); setEditing(null); setDraft(blank()); setReason(""); setFormOpen(false);
  });
  const decision = (row: CuratedRecord, action: string) => perform(async () => {
    if (!reason.trim()) throw Error("请先填写评审或撤回原因。");
    await apiPost(`/data-workbench/entries/${row.id}/review`, { action, expected_revision: row.revision, reason });
    setMessage("评审状态已保存；请在测评榜单查看已批准数据。");
  });
  const nominate = (report: Report) => perform(async () => {
    const run = await apiGet<{ snapshot: { parameters: Record<string, unknown>; manifest_sha256: string; tool: { revision: string } } }>(`/runs/${report.run_id}`);
    const entry = { source_key: `report-${report.id}`, source_type: "report", model_name: report.remote_model, dataset: "GPQA", metric: "accuracy", value: report.summary.accuracy, unit: "%", direction: "higher_is_better", scope: "lite", conditions: { ...run.snapshot.parameters, manifest_sha256: run.snapshot.manifest_sha256, tool_revision: run.snapshot.tool.revision }, source_name: "AutoEval", source_url: null, source_version: report.id, source_sha256: report.sha256, report_id: report.id, notes: "报告成绩提名，正式发布门禁不适用。" };
    await apiPost("/data-workbench/entries", entry); setTab("榜单数据维护"); setMessage("报告已提名，需评审后入榜。");
  });
  const importOfficial = () => perform(async () => {
    const batch = JSON.parse(importText);
    if (!Array.isArray(batch) || !batch.length || batch.length > 50) throw Error("每批须为 1–50 条 JSON 数组。");
    let completed = 0;
    for (const item of batch) {
      if (item.source_type !== "official") throw Error(`第 ${completed + 1} 条不是官方来源；此前 ${completed} 条已保存，可按来源键安全重试。`);
      try { await apiPost("/data-workbench/entries", fileHash ? { ...item, source_sha256: fileHash } : item); completed++; }
      catch { throw Error(`第 ${completed + 1} 条导入失败；此前 ${completed} 条已保存。检查字段及来源键后重试，已有相同内容不重复录入。`); }
    }
    setMessage(`${completed} 条官方数据已导入待评审，未自动入榜。`);
  });
  return <div className="stack data-workbench">
    <div className="notice">开发环境数据维护入口。平台报告与外部来源独立留痕；评审批准仅控制内部榜单展示，不代表正式 Release 通过。</div>
    <div className="workbench-toolbar"><div className="tabs">{["榜单数据维护", "模型列表", "数据集列表", "报告评审入榜", "官方数据导入", "标准仓输入", "环境诊断"].map((name) => <button key={name} className={tab === name ? "active" : ""} onClick={() => setTab(name)}>{name}</button>)}</div><button className="workbench-refresh" disabled={busy || loading} onClick={() => setRevision((v) => v + 1)}>刷新数据</button></div>
    {(loading || message) && <p role="status">{loading ? "数据加载中…" : message}</p>}
    {tab === "模型列表" && <WorkbenchModels revision={revision} />}
    {tab === "数据集列表" && <WorkbenchDatasets revision={revision} />}
    {tab === "标准仓输入" && <StandardInputs />}
    {tab === "环境诊断" && <><DiagnosticTasks /><FixturePipeline /></>}
    {tab === "报告评审入榜" && <section className="card"><h2>报告候选成绩</h2><p className="muted">仅完整、真实且原始证据可用的报告可提名；提名与批准时后台再次校验，不能修改原报告成绩。</p><div className="table-wrap"><table><thead><tr><th>模型 / 报告</th><th>分数</th><th>证据</th><th>操作</th></tr></thead><tbody>{reports.map((row) => <tr key={row.id}><td>{row.remote_model}<small>{row.id}</small></td><td>{row.evidence_status === "verified" ? row.summary.accuracy ?? "—" : "—"}</td><td>{row.evidence_status === "verified" ? "已校验" : "不可用"}</td><td><a className="link" href={`#report/${row.id}`}>查看报告</a> <button disabled={busy || row.validity !== "complete" || row.evidence_status !== "verified" || row.summary.accuracy === null || row.execution_origin !== "real"} onClick={() => void nominate(row)}>提名评审</button></td></tr>)}</tbody></table></div>{!loading && !reports.length && <p className="empty">暂无报告候选</p>}<OffsetPagination label="报告候选" offset={reportOffset} nextOffset={reportNext} onChange={setReportOffset} disabled={busy} /></section>}
    {tab === "榜单数据维护" && <>
      <section className="card"><div className="spread"><h2>榜单数据</h2><button onClick={() => { setEditing(null); setDraft(blank()); setReason(""); setFormOpen(true); }}>新建录入</button></div><label>评审 / 修改原因<input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="填写依据，批准、驳回、撤回和修改均记录原因" /></label><div className="table-wrap"><table><thead><tr><th>模型 / 数据集</th><th>分数</th><th>来源</th><th>状态 / 版本</th><th>操作</th></tr></thead><tbody>{rows.map((row) => <tr key={row.id}><td>{row.entry.model_name}<small>{row.entry.dataset} · {row.entry.metric} · {row.entry.scope}</small></td><td>{row.entry.value} {displayScoreUnit(row.entry.unit)}</td><td>{labels[row.entry.source_type]}<small>{row.entry.source_name} / {row.entry.source_version}</small></td><td>{labels[row.state]} · {row.revision}</td><td><ManagementDrawer title="来源与历史"><pre className="snapshot">{JSON.stringify(row.entry, null, 2)}</pre><EntryHistory id={row.id} revision={row.revision} /></ManagementDrawer> {row.entry.source_type === "report" && ["rejected", "withdrawn"].includes(row.state) && <button disabled={busy} onClick={() => void perform(async () => { if (!reason.trim()) throw Error("请填写重新提名原因"); await apiPost(`/data-workbench/entries/${row.id}/edit`, { expected_revision: row.revision, entry: row.entry, reason }); setMessage("已重新提交评审，报告原始成绩保持不变。"); })}>重新提名</button>} {row.state === "draft" && <><button disabled={busy} onClick={() => void decision(row, "approve")}>批准入榜</button><button disabled={busy} onClick={() => void decision(row, "reject")}>驳回</button></>}{row.state === "approved" ? <button disabled={busy} onClick={() => void decision(row, "withdraw")}>撤回</button> : <button disabled={busy || row.entry.source_type === "report"} onClick={() => { setEditing(row); setFormOpen(true); setDraft({ ...row.entry, value: String(row.entry.value), conditions: JSON.stringify(row.entry.conditions, null, 2), source_url: row.entry.source_url || "" }); }}>修改</button>}</td></tr>)}</tbody></table></div>{!loading && !rows.length && <p className="empty">暂无维护数据，请提名报告或录入外部成绩。</p>}<OffsetPagination label="维护数据" offset={offset} nextOffset={next} onChange={setOffset} disabled={busy} /></section>
      <details className="card" open={formOpen} onToggle={(e) => setFormOpen(e.currentTarget.open)}><summary>{editing ? "修改并重新评审" : "录入外部成绩"}</summary><form onSubmit={(e) => { e.preventDefault(); void save(); }}><div className="workbench-form">
        <label>模型标识<ModelIdentifierSelect value={draft.model_name} onChange={(id) => setDraft({ ...draft, model_name: id })} disabled={busy} /></label><label>来源类型<select value={draft.source_type} onChange={(e) => { if (e.target.value === "report") { setTab("报告评审入榜"); setMessage("请选择已有平台报告提名，成绩和证据由后台核验，无需手工填写。"); } else setDraft({ ...draft, source_type: e.target.value }); }}><option value="official">官方公开成绩</option><option value="report">平台实测报告</option></select></label>
        {([ ["source_key", "来源唯一键"], ["dataset", "数据集"], ["metric", "指标名称"], ["value", "成绩"], ["unit", "单位"], ["source_name", "来源名称"], ["source_url", "来源网址（官方必填）"], ["source_version", "来源版本 / 日期"], ["source_sha256", "原文 SHA-256"] ] as const).map(([key, label]) => <label key={key}>{label}<input required={key !== "source_url" || draft.source_type === "official"} disabled={key === "source_key" && !!editing} type={key === "value" ? "number" : key === "source_url" ? "url" : "text"} min={key === "value" ? 0 : undefined} step="any" value={draft[key]} onChange={(e) => setDraft({ ...draft, [key]: e.target.value })} /></label>)}
        <label>范围<select value={draft.scope} onChange={(e) => setDraft({ ...draft, scope: e.target.value })}><option value="full">全量</option><option value="lite">Lite</option></select></label><label>指标方向<select value={draft.direction} onChange={(e) => setDraft({ ...draft, direction: e.target.value })}><option value="higher_is_better">越高越好</option><option value="lower_is_better">越低越好</option></select></label>
      </div><label>比较条件（JSON，必填）<textarea required rows={4} placeholder={'填写样本范围、工具版本、精度；性能成绩另含硬件、并发及输入输出长度。缺失条件请明确写“未知”。'} value={draft.conditions} onChange={(e) => setDraft({ ...draft, conditions: e.target.value })} /></label><label>备注<textarea value={draft.notes} onChange={(e) => setDraft({ ...draft, notes: e.target.value })} /></label><button className="primary" disabled={busy}>保存待评审</button></form></details>
    </>}
    {tab === "官方数据导入" && <section className="card stack"><h2>官方来源 JSON 导入</h2><p>导入只创建待评审数据，不自动批准。支持手工整理的官方快照；未配置网站连接器，不执行网络自动同步。请核对来源权威性、版本和单位。</p><p className="muted">数组每项字段与外部成绩录入一致。source_type 须为 official，source_key 须稳定唯一；不同内容使用已有键会拒绝。</p><button onClick={() => { setFileHash(""); setImportText(JSON.stringify([{ ...blank(), source_key: "official-source-model-dataset-version", source_type: "official", value: null, conditions: { sample_scope: "请填写", tool_revision: "请填写" } }], null, 2)); }}>填入格式模板</button><input type="file" accept=".json,application/json" onChange={async (e) => { const file = e.target.files?.[0]; if (!file) return; if (file.size > 1024 * 1024) { setMessage("文件不能超过 1MB。"); return; } const bytes = await file.arrayBuffer(); const hash = await crypto.subtle.digest("SHA-256", bytes); setFileHash([...new Uint8Array(hash)].map((v) => v.toString(16).padStart(2, "0")).join("")); setImportText(new TextDecoder().decode(bytes)); }} /><textarea rows={14} aria-label="官方数据JSON" value={importText} onChange={(e) => { setImportText(e.target.value); setFileHash(""); }} /><small>文件导入时原文 SHA-256 使用上传文件字节的校验值；编辑文本后请自行提供原文校验值。</small><button className="primary" disabled={busy || !importText.trim()} onClick={() => void importOfficial()}>导入待评审</button></section>}
  </div>;
}

function EntryHistory({ id, revision }: { id: string; revision: number }) {
  const [events, setEvents] = useState<{ id: string; created_at: string; action: string; details: { reason: string; revision: number; entry: Entry } }[]>([]);
  const [message, setMessage] = useState("加载历史…");
  useEffect(() => { const c = new AbortController(); apiGet<typeof events>(`/audit-events?resource_id=${id}&limit=100`, c.signal).then((rows) => { setEvents(rows); setMessage(rows.length ? "最近 100 条变更" : "暂无历史"); }).catch(() => { if (!c.signal.aborted) setMessage("历史读取失败"); }); return () => c.abort(); }, [id, revision]);
  return <section><h3>评审与修改记录</h3><p>{message}</p>{events.map((event) => <details key={event.id}><summary>{new Date(event.created_at).toLocaleString("zh-CN")} · 版本 {event.details.revision} · {event.details.reason}</summary><pre className="snapshot">{JSON.stringify(event.details.entry, null, 2)}</pre></details>)}</section>;
}

const datasetPriority = (name: string) => {
 const value = name.toLowerCase().replace(/[^a-z0-9]/g, "");
 return value.includes("gpqadiamond") ? 0 : value.includes("deepswe") ? 1 : value.includes("terminalbench") ? 2 : value.includes("automationbench") ? 3 : value.includes("mmmupro") ? 4 : value.includes("gpqa") ? 5 : 6;
};
export function PublishedLeaderboard() {
  const [groups, setGroups] = useState<{ id: string; comparison: Record<string, unknown>; entries: CuratedRecord[] }[]>([]);
  const [message, setMessage] = useState("加载已评审榜单…");
  const [revision, setRevision] = useState(0);
  useEffect(() => { const c = new AbortController(); apiGet<typeof groups>("/data-workbench/published", c.signal).then((data) => { setGroups(data); setMessage(data.length ? "" : "暂无已批准的榜单数据，请在数据工作台录入并评审。"); }).catch(() => { if (!c.signal.aborted) setMessage("已评审榜单读取失败，请检查服务及迁移。"); }); return () => c.abort(); }, [revision]);
  const [mode, setMode] = useState("排行榜");
  const [dataset, setDataset] = useState("");
  const [datasetInitialized, setDatasetInitialized] = useState(false);
  useEffect(() => {
    if (datasetInitialized || !groups.length) return;
    const names = [...new Set(groups.map((g) => datasetIdentity(String(g.comparison.dataset))))];
    const preferred = names.find((name) => /gpqa.*diamond/i.test(name)) || names.find((name) => /^GPQA/i.test(name));
    if (preferred) setDataset(preferred);
    setDatasetInitialized(true);
  }, [groups, datasetInitialized]);
  const [metric, setMetric] = useState("");
  const [source, setSource] = useState("");
  const [query, setQuery] = useState("");
  const merged = new Map<string, { id: string; comparison: Record<string, unknown>; entries: CuratedRecord[] }>();
  for (const original of groups) {
    const group = { ...original, comparison: { ...original.comparison, dataset: datasetIdentity(String(original.comparison.dataset)) } as Record<string, unknown>, entries: original.entries.filter((row) => row.entry.source_type !== "manual") };
    if (!group.entries.length) continue;
    const key = JSON.stringify([group.comparison.dataset, group.comparison.metric, group.comparison.unit, group.comparison.direction]);
    const existing = merged.get(key);
    if (existing) existing.entries.push(...group.entries);
    else merged.set(key, { id: key, comparison: { ...group.comparison, source_name: "平台实测与官方公开成绩", source_type: "mixed", scope: "各记录范围见模型说明" }, entries: [...group.entries] });
  }
  const filtered = [...merged.values()].sort((a,b) => datasetPriority(String(a.comparison.dataset))-datasetPriority(String(b.comparison.dataset))).filter((g) => (!dataset || g.comparison.dataset === dataset) && (!metric || g.comparison.metric === metric)).map((g) => ({ ...g, entries: g.entries.filter((r) => (!source || r.entry.source_type === source) && r.entry.model_name.toLowerCase().includes(query.toLowerCase())).sort((a,b) => (g.comparison.direction === "higher_is_better" ? b.entry.value-a.entry.value : a.entry.value-b.entry.value) || a.entry.model_name.localeCompare(b.entry.model_name)) })).filter((g) => g.entries.length);

  return <div className="stack" data-testid="published-leaderboard">
    <div className="spread"><div className="tabs" aria-label="榜单显示模式">{["排行榜", "图表"].map((value) => <button key={value} className={mode === value ? "active" : ""} aria-pressed={mode === value} onClick={() => setMode(value)}>{value}</button>)}</div><button onClick={() => setRevision((v) => v + 1)}>刷新榜单</button></div>
    <section className="card filters leaderboard-filters">
      <label className="select-field"><span>数据集</span><select aria-label="榜单数据集" value={dataset} onChange={(e) => setDataset(e.target.value)}><option value="">全部数据集</option>{[...new Set(groups.map((g) => datasetIdentity(String(g.comparison.dataset))))].sort((a,b) => datasetPriority(a) - datasetPriority(b) || a.localeCompare(b)).map((v) => <option key={v}>{v}</option>)}</select></label>
      <label className="select-field"><span>指标</span><select value={metric} onChange={(e) => setMetric(e.target.value)}><option value="">全部指标</option>{[...new Set(groups.map((g) => String(g.comparison.metric)))].sort().map((v) => <option key={v}>{v}</option>)}</select></label>
      <label className="select-field"><span>来源</span><select value={source} onChange={(e) => setSource(e.target.value)}><option value="">全部来源</option><option value="official">官方公开成绩</option><option value="report">平台报告</option></select></label>
      <input aria-label="搜索榜单模型" placeholder="搜索模型标识" value={query} onChange={(e) => setQuery(e.target.value)} />
    </section>
    <div className="notice">同一数据集与指标下同时展示已评审的平台实测和官方成绩，以来源颜色区分。Lite、全量及思考模式逐条标注；范围或条件不同仅作参考，不代表严格同条件排名。</div>
    {message && <p role="status">{message}</p>}
    {!message && !filtered.length && <p className="empty">没有符合筛选条件的数据</p>}
    {filtered.map((group) => {
      const max = Math.max(...group.entries.map((r) => r.entry.value), 1);
      return <section className="card" key={group.id}>
        <div className="spread"><h3>{String(group.comparison.dataset)} · {String(group.comparison.metric)}</h3><span className="badge">参考对比</span></div>
        <p className="muted">{String(group.comparison.source_name)} · {String(group.comparison.scope)} · {group.comparison.direction === "higher_is_better" ? "越高越好" : "越低越好"} · {new Set(group.entries.map((row) => row.entry.model_name)).size} 个模型 · {group.entries.length} 条成绩</p>
        {mode === "排行榜" ? <div className="table-wrap"><table className="leader-table leaderboard-compact"><thead><tr><th>参考顺序</th><th>模型标识</th><th>模型发布时间</th><th>评测时间</th><th>参数量</th><th>分数</th><th>测试方法</th><th>来源</th></tr></thead><tbody>{group.entries.map((row, index) => <tr key={row.id}><td><span className={`rank rank-${index}`}>{String(index + 1).padStart(2,"0")}</span></td><td><b className="model-with-logo"><VendorLogo model={row.entry.model_name} />{row.entry.model_name}</b><span className="leaderboard-model-tags" title={`${labels[row.entry.source_type]} · ${row.entry.scope} · ${row.entry.conditions.model_identity_status || ""}`}><span>{displayMetadata(row.entry.conditions.mode)}</span>{row.entry.scope === "lite" && <span>Lite</span>}</span></td><td>{displayMetadata(row.entry.conditions.model_release_date)}</td><td>{displayMetadata(row.entry.conditions.evaluated_at)}</td><td>{displayMetadata(row.entry.conditions.parameter_count)}</td><td><div className="inline score"><b>{row.entry.value} {displayScoreUnit(row.entry.unit)}</b><progress max={max} value={row.entry.value} aria-label={`${row.entry.model_name} 成绩`} /></div></td><td><ManagementDrawer title="查看"><h3>测试方法</h3><p>来源：{row.entry.source_name}</p><p>版本：{row.entry.source_version}</p><p>原始数据集 / 方法：{row.entry.dataset}</p><p>指标：{row.entry.metric}；范围：{row.entry.scope}</p><pre className="snapshot">{JSON.stringify(row.entry.conditions,null,2)}</pre><p>方法详情暂按来源披露信息展示。</p></ManagementDrawer></td><td>{row.entry.report_id ? <a href={`#report/${row.entry.report_id}`}>来源报告</a> : row.entry.source_url ? <a href={row.entry.source_url} target="_blank" rel="noreferrer">官方来源</a> : row.entry.source_name}</td></tr>)}</tbody></table></div> : <figure className="leaderboard-chart leaderboard-chart-refined" aria-label={`${String(group.comparison.dataset)} 模型成绩对比`}><figcaption className="leaderboard-chart-toolbar"><span className="muted">分数{displayScoreUnit(group.comparison.unit) ? `（${displayScoreUnit(group.comparison.unit)}）` : ""} · 纵轴从 0 开始</span><span className="chart-legend"><span><i className="chart-swatch measured" />平台实测</span><span><i className="chart-swatch official" />官方成绩</span></span></figcaption><div className="vertical-chart-scroll"><div className={`vertical-chart${group.entries.length > 8 ? " vertical-chart-dense" : ""}`} style={{minWidth: `${Math.max(360, group.entries.length * (group.entries.length > 8 ? 48 : 100))}px`}}><div className="vertical-chart-scale" aria-hidden="true">{[1,.75,.5,.25,0].map((ratio) => <span key={ratio}>{Number((max*ratio).toFixed(2))}</span>)}</div><div className="vertical-chart-columns">{group.entries.map((row) => <div className="vertical-chart-column" key={row.id}><div className="vertical-chart-plot"><div className={`vertical-chart-bar source-${row.entry.source_type}`} style={{height:`${row.entry.value/max*100}%`}} role="meter" aria-label={row.entry.model_name} aria-valuemin={0} aria-valuemax={max} aria-valuenow={row.entry.value}><strong>{row.entry.value} {displayScoreUnit(row.entry.unit)}</strong></div></div><div className="vertical-chart-label" title={`${row.entry.model_name} · ${displayMetadata(row.entry.conditions.mode)} · ${row.entry.value} ${displayScoreUnit(row.entry.unit)}`}><b><VendorLogo model={row.entry.model_name} />{row.entry.model_name.split("/").pop()}</b><small>{displayMetadata(row.entry.conditions.mode)} · {row.entry.scope === "lite" ? "Lite" : "全量 / 官方范围"}</small></div></div>)}</div></div></div></figure>}
        <details><summary>比较条件与来源版本</summary><pre className="snapshot">{JSON.stringify(group.entries.map((row) => ({ model: row.entry.model_name, original_dataset: row.entry.dataset, source: row.entry.source_url, version: row.entry.source_version, conditions: row.entry.conditions })),null,2)}</pre></details>
      </section>;
    })}
  </div>;
}
