import { ProbeProgress } from "./ProbeProgress";
import { useEffect, useRef, useState } from "react";
import { apiGet, apiGetPage, apiPost } from "../services/api";
import { type Resource, type Target, useOptions } from "../services/evaluation";
import { ManagementDrawer } from "./ManagementDrawer";
import { StandardInputs } from "./StandardInputs";
import { ExecutionBindings } from "./ExecutionBindings";
import { DatasetManifests } from "./DatasetManifests";

type Report = {
  input_sha256: string; conditional: boolean; confidence: string; warnings: string[];
  representative_duration_sec: number;
  estimate: { total_duration_sec: number; range: { p10_sec: number; p90_sec: number } };
  plan: { dataset_order: string[]; dataset_concurrency: number; case_concurrency: Record<string, number> };
  tasks: { task_id: string; standalone_duration_sec: number; scheduled_duration_sec: number; scheduled_start_sec: number; scheduled_end_sec: number }[];
};
type Options = {
  compilations: { adaptor_revision?: string; runtime_config?: Record<string, unknown>; id: string; label?: string; sha256: string; target_id: string; dataset: string; input_ready: boolean; config: { subcommand: string; params: Record<string, unknown> } }[];
  manifests: { id: string; dataset: string; scope: string; sample_count: number; sha256?: string }[];
  curves: { id: string; target_id: string; resource_id: string; resource_kind: string; measured_at: string }[];
};
type DatasetCheck = { task_id: string; dataset: string; data_path: string | null; tool_revision: string | null; expected_tool_revision: string; blocking_reasons: string[] };
type Job = { id: string; state: string; created_at?: string; updated_at?: string; result?: { progress?: { stage: string; current_concurrency?: number | null; completed_levels: number; total_levels: number; observed_at?: string } | null; report?: Report; throughput_profile_id?: string; reason?: string; execution_ready?: boolean; supported_datasets?: string[]; blocking_reasons?: string[]; dataset_checks?: DatasetCheck[]; tool_versions?: Record<string, { expected: string; actual: string | null }> } };
const datasets = ["GPQA", "MMMU-Pro", "DeepSWE"];
const duration = (seconds: number | null | undefined) => seconds == null ? "未知" : `${Math.floor(seconds / 3600)}小时 ${Math.floor(seconds % 3600 / 60)}分 ${Math.round(seconds % 60)}秒`;

export function PlanEstimate({ report }: { report: Report }) {
  return <section className="stack" aria-label="耗时估算与编排建议">
    <h3>预计总耗时：{duration(report.estimate.total_duration_sec)}</h3>
    <p>p10–p90：{duration(report.estimate.range.p10_sec)} – {duration(report.estimate.range.p90_sec)}；置信度：{report.confidence}</p>
    <p>不含平台排队及报告分析耗时。{report.conditional && "当前为条件估算，需执行资源预检后重新规划。"}</p>
    <p>执行顺序：{report.plan.dataset_order.join(" → ")}；同时运行 {report.plan.dataset_concurrency} 个数据集。</p>
    <table><thead><tr><th>数据集</th><th>case 并发</th><th>单独执行耗时</th><th>计划内耗时</th><th>开始 → 结束</th></tr></thead>
      <tbody>{report.tasks.map(t => <tr key={t.task_id}><td>{t.task_id}</td><td>{report.plan.case_concurrency[t.task_id]}</td><td>{duration(t.standalone_duration_sec)}</td><td>{duration(t.scheduled_duration_sec)}</td><td>{duration(t.scheduled_start_sec)} → {duration(t.scheduled_end_sec)}</td></tr>)}</tbody></table>
    <p>代表性仿真时间线，终点 {duration(report.representative_duration_sec)}；并行数据集共享吞吐与资源，各项时长不能相加作为总耗时。</p>
    {report.warnings?.map((w, i) => <small key={i}>{w}</small>)}
  </section>;
}

export function TaskPlanning() {
  const targets = useOptions<Target>("/target-bindings");
  const docker = useOptions<Resource>("/resources");
  const ssh = useOptions<Resource>("/ssh-resources");
  const [target, setTarget] = useState("");
  const [resourceKind, setResourceKind] = useState("docker_local");
  const [resource, setResource] = useState("");
  const [options, setOptions] = useState<Options>({ compilations: [], manifests: [], curves: [] });
  const [selection, setSelection] = useState<Record<string, { compilation: string; manifest: string }>>({});
  const [enabled, setEnabled] = useState<string[]>([]);
  const [curve, setCurve] = useState("");
  const [parallel, setParallel] = useState(1);
  const [recommend, setRecommend] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [probe, setProbe] = useState<Job | null>(null);
  const [preflight, setPreflight] = useState<Job | null>(null);
  const [execution, setExecution] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [probeConfirmed, setProbeConfirmed] = useState(false);
  const [runConfirmed, setRunConfirmed] = useState(false);
  const [curveText, setCurveText] = useState("");
  const [levels, setLevels] = useState("1,2,4,8");
  const [promptTokens, setPromptTokens] = useState(512);
  const [maxTokens, setMaxTokens] = useState(512);
  const generation = useRef(0);
  const probeIntent = useRef<{ body: string; key: string } | null>(null);
  const pending = useRef<{ body: string; key: string } | null>(null);
  const resources = resourceKind === "customer_ssh" ? ssh : docker;
  const refresh = async () => {
    const collected: Options = { compilations: [], manifests: [], curves: [] };
    let offset: number | null = 0;
    while (offset != null) {
      const page: { data: Options; next_offset: number | null } = await apiGetPage<Options>(`/planning/options?offset=${offset}&limit=100`);
      collected.compilations.push(...page.data.compilations);
      collected.manifests.push(...page.data.manifests);
      collected.curves.push(...page.data.curves);
      if (page.next_offset != null && page.next_offset <= offset) throw new Error("规划选项分页异常");
      offset = page.next_offset;
    }
    const stable = (value: unknown): string => {
      if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
      if (value && typeof value === "object") return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => `${JSON.stringify(k)}:${stable(v)}`).join(",")}}`;
      return JSON.stringify(value);
    };
    const seen = new Set<string>();
    collected.compilations = collected.compilations.filter(c => {
      const params = Object.fromEntries(Object.entries(c.config.params).filter(([key]) => !["work_dir", "container_name"].includes(key)));
      const key = stable({ target: c.target_id, dataset: c.dataset, subcommand: c.config.subcommand, adaptor: c.adaptor_revision, runtime: c.runtime_config, params });
      if (seen.has(key)) return false;
      seen.add(key); return true;
    });
    const manifests = new Set<string>();
    collected.manifests = collected.manifests.filter(m => {
      const key = stable({ dataset: m.dataset, scope: m.scope, hash: m.sha256 || m.id });
      if (manifests.has(key)) return false;
      manifests.add(key); return true;
    });
    setOptions(collected);
  };
  useEffect(() => { void refresh().catch(() => setMessage("规划选项加载失败，请重试。")); }, []);
  const payload = { target_id: target, resource_id: resource, resource_kind: resourceKind,
    throughput_profile_id: curve || null, dataset_concurrency: parallel, recommend,
    tasks: datasets.filter(d => enabled.includes(d) && selection[d]?.compilation && selection[d]?.manifest).map(d => ({
      task_id: d.replaceAll("-", "_"), dataset: d, compilation_id: selection[d].compilation, manifest_id: selection[d].manifest,
    })) };
  const fingerprint = JSON.stringify(payload);
  const preflightFingerprint = JSON.stringify({ target_id: target, resource_id: resource, resource_kind: resourceKind, tasks: payload.tasks });
  useEffect(() => {
    generation.current++; setJob(null); setExecution(""); setRunConfirmed(false);
  }, [fingerprint]);
  useEffect(() => { setPreflight(null); }, [preflightFingerprint]);
  useEffect(() => { setPreflight(null); setProbeConfirmed(false); setCurve(""); }, [target, resource, resourceKind]);
  useEffect(() => {
    const jobs = [job, probe, preflight].filter((j): j is Job => !!j && ["pending", "running"].includes(j.state));
    if (!jobs.length) return;
    let stopped = false;
    const timer = setTimeout(() => {
      void Promise.all(jobs.map(j => apiGet<Job>(`/planning/probes/${j.id}`))).then(rows => {
        if (stopped) return;
        for (const row of rows) {
          if (row.id === job?.id) setJob(row);
          if (row.id === preflight?.id) setPreflight(row);
          if (row.id === probe?.id) { setProbe(row); if (row.result?.throughput_profile_id) { void refresh(); setCurve(row.result.throughput_profile_id); } }
        }
      }).catch(() => { if (!stopped) setMessage("后台状态暂不可读，请刷新查证，勿重复启动探测。"); });
    }, 2000);
    return () => { stopped = true; clearTimeout(timer); };
  }, [job, probe, preflight]);
  const action = async (operation: () => Promise<void>) => {
    setBusy(true); setMessage("");
    try { await operation(); } catch (error) { setMessage(error instanceof Error ? error.message : "操作失败"); }
    finally { setBusy(false); }
  };
  const probeLevels = levels.split(",").map(Number);
  const validLevels = probeLevels.length > 0 && probeLevels.every((n, i) => Number.isInteger(n) && n >= 1 && n <= 64 && (i === 0 || n > probeLevels[i - 1]));
  const requestBudget = 2 * probeLevels.reduce((sum, n) => sum + Math.max(2, n), 0);
  const incomplete = enabled.some(d => !selection[d]?.compilation || !selection[d]?.manifest);
  const ready = !!target && !!resource && payload.tasks.length > 0 && !incomplete;
  const report = job?.state === "completed" ? job.result?.report : undefined;
  const checking = !!preflight && ["pending", "running"].includes(preflight.state);
  const preflightLabel = !preflight ? "未检查" : ({ pending: "排队中", running: "检查中", completed: preflight.result?.execution_ready ? "检查通过" : "存在阻塞", failed: "检查失败", cancelled: "已取消" } as Record<string, string>)[preflight.state] || "状态待确认";
  const preflightTone = checking ? "checking" : preflight?.result?.execution_ready ? "passed" : "blocked";
  const blocker = !target ? "请选择模型服务" : !resource ? "请选择执行资源" : !enabled.length ? "请启用至少一个数据集" : incomplete ? "请补全已启用数据集的配置与样本范围" : checking ? "正在检查执行资源，请稍候…" : !preflight?.result?.execution_ready ? "请完成资源预检并处理阻塞项" : !runConfirmed ? "请确认测评范围与模型调用费用" : "配置已就绪，可以提交测评";
  const sampleCount = payload.tasks.reduce((sum, t) => sum + (options.manifests.find(m => m.id === t.manifest_id)?.sample_count || 0), 0);
  return <div className="planning-layout"><section className="card stack planning-card" aria-label="完整测评规划">
    <h2>1. 模型与执行资源</h2>
    <p className="planning-intro">支持 GPQA、MMMU-Pro、DeepSWE 的全量或已绑定 Lite 清单。AutomationBench 待来源与评分核验。</p>
    <div className="planning-fields">
      <label><span>模型服务</span><select aria-label="规划模型服务" value={target} onChange={e => { setTarget(e.target.value); setSelection({}); }}><option value="">请选择</option>{targets.rows.map(t => <option key={t.id} value={t.id}>{t.remote_model} / {t.base_url}</option>)}</select></label>
      <label><span>执行环境</span><select value={resourceKind} onChange={e => { setResourceKind(e.target.value); setResource(""); }}><option value="docker_local">Docker</option><option value="customer_ssh">客户 SSH</option></select></label>
      <label><span>执行资源</span><select aria-label="规划执行资源" value={resource} onChange={e => setResource(e.target.value)}><option value="">请选择</option>{resources.rows.map(r => <option key={r.id} value={r.id}>{r.binding_key}</option>)}</select></label>
    </div>
    {(targets.error || resources.error) && <p role="alert">{targets.error || resources.error}</p>}
    <div className="planning-actions">
      <button disabled={busy || !ready || checking} onClick={() => void action(async () => { setPreflight(await apiPost<Job>("/planning/resources/preflight", payload)); })}>{checking ? "正在预检…" : preflight ? "重新预检" : "预检执行资源"}</button>
      {preflight && <div className="preflight-feedback" role="status" aria-live="polite"><span className={`preflight-status ${preflightTone}`}><i aria-hidden="true" />{preflightLabel}</span><span className="preflight-hint">{checking ? "正在核对工具版本、数据路径与执行环境" : preflight.result?.execution_ready ? `可执行：${preflight.result.supported_datasets?.join("、") || "所选数据集"}` : preflight.state === "completed" ? "请查看下方检查明细，处理阻塞后重新预检" : "请检查连接及后台任务状态后重试"}</span></div>}
    </div>
    {preflight?.state === "completed" && preflight.result && <div className="notice">
      <p>Planner：{preflight.result.tool_versions?.planner.actual?.slice(0, 12) || "未安装或版本未知"}；Adaptor：{preflight.result.tool_versions?.adaptor.actual?.slice(0, 12) || "未安装或版本未知"}</p>
      {!!preflight.result.blocking_reasons?.length && <p>阻塞原因：{preflight.result.blocking_reasons.join("、")}</p>}
      {preflight.result.dataset_checks?.map(check => <p key={check.task_id}>{check.dataset} · 数据路径：{check.data_path || "未配置"} · 工具版本：{check.tool_revision?.slice(0, 12) || "校验未通过"}{check.blocking_reasons.length ? ` · ${check.blocking_reasons.join("、")}` : " · 就绪"}</p>)}
    </div>}
    <h2>2. 数据集与样本范围</h2>
    <div className="table-wrap"><table className="planning-table"><thead><tr><th>启用</th><th>数据集</th><th>执行配置</th><th>样本范围</th><th>状态</th></tr></thead><tbody>{datasets.map(d => {
      const active = enabled.includes(d);
      const configs = options.compilations.filter(c => c.target_id === target && c.input_ready && c.dataset === d);
      return <tr key={d}><td><input type="checkbox" aria-label={`启用 ${d}`} checked={active} onChange={e => { setEnabled(rows => e.target.checked ? [...rows, d] : rows.filter(row => row !== d)); setSelection(rows => ({ ...rows, [d]: { compilation: "", manifest: "" } })); }} /></td><td>{d}</td><td data-label="执行配置"><select disabled={!active || !target} aria-label={`${d} 执行配置`} value={selection[d]?.compilation || ""} onChange={e => setSelection(rows => ({ ...rows, [d]: { ...rows[d], compilation: e.target.value } }))}><option value="">{!target ? "先选择模型服务" : !configs.length ? "暂无可用配置" : "请选择执行配置"}</option>{configs.map(c => <option key={c.id} value={c.id}>{c.label || c.sha256.slice(0, 12)} / {String(c.config.params.batch_size ?? c.config.params.n_concurrent_trials ?? 1)} 并发</option>)}</select></td><td data-label="样本范围"><select disabled={!active || !selection[d]?.compilation} aria-label={`${d} 样本范围`} value={selection[d]?.manifest || ""} onChange={e => setSelection(rows => ({ ...rows, [d]: { ...rows[d], manifest: e.target.value } }))}><option value="">请选择已绑定清单</option>{options.manifests.filter(m => m.dataset === d).map(m => <option key={m.id} value={m.id}>{m.scope === "full" ? "全量" : "Lite"} · {m.sample_count} 个样本 · {m.id.slice(0, 8)}</option>)}</select></td><td><span className="planning-row-state">{!active ? "未启用" : !configs.length ? "需配置" : selection[d]?.compilation && selection[d]?.manifest ? "已配置" : "待补全"}</span></td></tr>;
    })}</tbody></table></div>
    <details className="planning-management"><summary>管理配置与清单 · 缺少配置时在此维护</summary>
    <div className="inline"><ManagementDrawer title="配置标准输入"><StandardInputs /></ManagementDrawer><ManagementDrawer title="管理样本清单"><DatasetManifests /></ManagementDrawer><ManagementDrawer title="登记执行规则"><ExecutionBindings /></ManagementDrawer><button onClick={() => void action(refresh)}>刷新配置与清单</button></div>
    </details>
    <p className="muted">执行配置来自标准输入编译；样本清单须绑定已登记的工具与评分规则。</p>
    {incomplete && <p role="alert">请为每个已选执行配置选择样本范围。</p>}
    <h2>3. 执行策略</h2>
    <div className="planning-strategy"><label>数据集同时运行数<input aria-label="数据集同时运行数" type="number" min={1} max={3} value={parallel} onChange={e => setParallel(Number(e.target.value))} /></label>
    <label><input type="checkbox" checked={recommend} onChange={e => setRecommend(e.target.checked)} />可选：搜索更优顺序和并发（不影响直接测评）</label></div>
    <details className="planning-throughput"><summary>吞吐曲线与主动探测<span>高级设置 · 可使用已有曲线或发起真实探测</span></summary><div className="probe-content"><h3>主动探测</h3><div className="probe-fields">
      <label>并发档位<input value={levels} onChange={e => { setLevels(e.target.value); setProbeConfirmed(false); }} /></label>
      <label>输入 token<input type="number" min={1} max={4096} value={promptTokens} onChange={e => { setPromptTokens(Number(e.target.value)); setProbeConfirmed(false); }} /></label>
      <label>最大输出 token<input type="number" min={1} max={4096} value={maxTokens} onChange={e => { setMaxTokens(Number(e.target.value)); setProbeConfirmed(false); }} /></label>
      </div><p className="probe-budget">包含非流式回退在内最多 {validLevels ? requestBudget : "—"} 次请求，最多 {validLevels ? requestBudget * maxTokens : "—"} 个输出 token；可能产生模型调用费用。</p>
      <div className="probe-actions"><label><input type="checkbox" checked={probeConfirmed} onChange={e => setProbeConfirmed(e.target.checked)} />确认探测预算</label>
      <button disabled={busy || !target || !resource || !probeConfirmed || !validLevels || probe?.state === "pending" || probe?.state === "running"} onClick={() => void action(async () => { const body = { target_id: target, resource_id: resource, resource_kind: resourceKind, concurrency: probeLevels, prompt_tokens: promptTokens, max_tokens: maxTokens }; const serialized = JSON.stringify(body); if (probeIntent.current?.body !== serialized || (probe && ["completed", "failed", "cancelled"].includes(probe.state))) probeIntent.current = { body: serialized, key: crypto.randomUUID() }; setProbe(await apiPost<Job>("/planning/probes", body, probeIntent.current.key)); })}>启动真实吞吐探测</button></div>
      {probe && <ProbeProgress job={probe} />}
      {probe && ["pending", "running"].includes(probe.state) && <button onClick={() => void action(async () => { await apiPost(`/planning/probes/${probe.id}/cancel`, {}); })}>取消探测</button>}
      <details className="probe-import"><summary>导入已有吞吐曲线</summary><div className="probe-import-content"><label>曲线 JSON<textarea aria-label="曲线 JSON" rows={5} placeholder="在此粘贴吞吐曲线 JSON" spellCheck={false} value={curveText} onChange={e => setCurveText(e.target.value)} /></label>
      <button disabled={busy || !target || !resource || !curveText} onClick={() => void action(async () => { const saved = await apiPost<{ id: string }>("/planning/throughput-profiles", { target_id: target, resource_id: resource, resource_kind: resourceKind, curve: JSON.parse(curveText) }); await refresh(); setCurve(saved.id); })}>导入吞吐曲线</button></div></details>
    </div></details>
    <label>吞吐来源<select aria-label="吞吐来源" value={curve} onChange={e => setCurve(e.target.value)}><option value="">请选择或探测</option>{options.curves.filter(c => c.target_id === target && c.resource_id === resource && c.resource_kind === resourceKind).map(c => <option key={c.id} value={c.id}>{new Date(c.measured_at).toLocaleString()} / {c.id.slice(0, 8)}</option>)}</select></label>
    </section><aside className="card stack planning-summary" aria-label="任务摘要">
    <h2>任务摘要</h2>
    <dl className="planning-checklist">
      <div><dt>模型服务</dt><dd>{targets.rows.find(t => t.id === target)?.remote_model || "待选择"}</dd></div>
      <div><dt>执行资源</dt><dd>{resources.rows.find(r => r.id === resource)?.binding_key || "待选择"}</dd></div>
      <div><dt>测评范围</dt><dd>{enabled.length} 个数据集 · {sampleCount} 个样本</dd></div>
      <div><dt>资源检查</dt><dd><span className={`preflight-status ${preflight ? preflightTone : "idle"}`}>{preflightLabel}</span></dd></div>
    </dl>
    <div className="planning-duration" role="status"><span>预计总耗时</span><strong>{report ? duration(report.estimate.total_duration_sec) : "暂无可靠估算"}</strong><small>{report ? "不含排队及报告分析耗时" : !curve ? "未提供吞吐曲线；可直接按当前配置发起测评" : "配置完成后点击计算耗时与编排"}</small></div>
    <button disabled={busy || !ready || !curve} onClick={() => void action(async () => { const revision = generation.current; const next = await apiPost<Job>("/planning/plans", payload); if (revision === generation.current) setJob(next); })}>计算耗时与编排</button>
    {job && <p role="status">规划：{job.state} {job.result?.reason}</p>}
    {report && <><details><summary>查看耗时与编排明细</summary><div className="table-wrap"><PlanEstimate report={report} /></div></details><button disabled={busy || report.conditional} onClick={() => void action(async () => { const revision = generation.current; const saved = await apiPost<{ execution_plan_id: string; report: Report }>(`/planning/plans/${job!.id}/apply`, { input_sha256: report.input_sha256 }); if (revision === generation.current) { setExecution(saved.execution_plan_id); setJob(current => current ? { ...current, result: { ...current.result, report: saved.report } } : current); } })}>采纳本次执行方案</button></>}
    {execution && <p role="status">已采纳；修改输入将使方案失效。</p>}
    <p className="planning-blocker" role="status">{blocker}</p>
    <label className="planning-confirm"><input type="checkbox" checked={runConfirmed} onChange={e => setRunConfirmed(e.target.checked)} />确认所选数据集及范围，提交真实测评，可能产生模型调用费用</label>
    <button className="primary" disabled={busy || !ready || !runConfirmed || !preflight?.result?.execution_ready} onClick={() => void action(async () => {
      const submission = execution ? payload : { ...payload, recommend: false, throughput_profile_id: null };
      const body = JSON.stringify({ payload: submission, execution });
      if (pending.current?.body !== body) pending.current = { body, key: crypto.randomUUID() };
      const saved = await apiPost<{ run_id: string }>(`/planning/runs${execution ? `?execution_plan_id=${execution}` : ""}`, submission, pending.current.key);
      pending.current = null; location.hash = `#planned-run/${saved.run_id}`;
    })}>提交测评{execution ? "（已采纳编排）" : "（当前配置）"}</button>
    {message && <p role="alert">{message}</p>}
  </aside></div>;
}

type PlannedRun = { report_id?: string | null; id: string; state: string; execution_origin: string; estimate: Report | null; report: { actual_duration_sec: number | null } | null; datasets: { id: string; dataset: string; state: string; summary?: { observed_cases?: number; expected_cases?: number; scored_cases?: number; accuracy?: number | null; diagnostic?: string; progress?: { stage: string; observed_at: string } }; actual_duration_sec: number | null; started_at: string | null; finished_at: string | null }[] };
export function PlannedRunView({ runId }: { runId: string }) {
  const [run, setRun] = useState<PlannedRun | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = () => { void apiGet<PlannedRun>(`/planning/runs/${runId}`).then(r => { if (!stopped) { setRun(r); setError(""); if (!["completed", "failed", "cancelled"].includes(r.state)) timer = setTimeout(poll, 2000); } }).catch(() => { if (!stopped) { setError("任务状态暂不可读，正在重试。"); timer = setTimeout(poll, 5000); } }); };
    poll(); return () => { stopped = true; clearTimeout(timer); };
  }, [runId]);
  const labels: Record<string, string> = { accepted: "已接收", queued: "排队中", pending: "待执行", submitting: "准备执行", running: "执行中", succeeded: "已完成", completed: "已完成", failed: "执行失败", cancelled: "已取消", unresolved: "状态待查证" };
  return <section className="card stack planned-run-detail"><div className="spread"><a className="link" href="#tasks">返回任务列表</a>{run?.report_id && <a className="primary planned-report-link" href={`#report/${run.report_id}`}>查看报告</a>}</div><h2>编排测评任务</h2>{error && <p role="alert">{error}</p>}{run ? <>
    <div className="spread"><code>{run.id}</code><span className={`planned-state state-${run.state}`}>{labels[run.state] || run.state}</span></div>
    <p className="muted">真实执行 · {run.datasets.length} 个数据集 · 实际耗时 {duration(run.report?.actual_duration_sec)}</p>
    {run.estimate ? <PlanEstimate report={run.estimate} /> : <p className="muted">按当前配置直接执行，未生成耗时估算。</p>}
    <div className="planned-stage-track" aria-label="执行阶段">{["任务接收", "资源调度", "工具执行", "报告归档"].map((label, index) => <span key={label} className={index === 0 || (index < 3 && run.datasets.some(d => !!d.started_at)) || !!run.report ? "reached" : ""}>{index + 1}. {label}</span>)}</div>
    <div className="table-wrap"><table><thead><tr><th>数据集</th><th>状态 / 进度</th><th>已归档样本</th><th>实际耗时</th><th>开始 / 结束</th></tr></thead><tbody>{run.datasets.map(d => <tr key={d.id}><td>{d.dataset}</td><td><span className={`planned-state state-${d.state}`}>{labels[d.state] || d.state}</span><small>{d.summary?.progress ? "工具进程已启动，等待样本结果归档" : !d.finished_at ? "等待后台状态更新" : "执行已结束"}</small></td><td>{d.summary?.observed_cases != null ? `${d.summary.observed_cases} / ${d.summary.expected_cases ?? "—"}` : "尚未归档，进度未知"}</td><td>{duration(d.actual_duration_sec)}</td><td>{d.started_at ? new Date(d.started_at).toLocaleString("zh-CN") : "未开始"}<small>{d.finished_at ? new Date(d.finished_at).toLocaleString("zh-CN") : "未结束"}</small></td></tr>)}</tbody></table></div>
    {run.datasets.some(d => d.summary?.diagnostic) && <details><summary>失败诊断</summary>{run.datasets.map(d => d.summary?.diagnostic && <pre key={d.id} className="snapshot">{d.summary.diagnostic}</pre>)}</details>}
    {!["completed", "failed", "cancelled"].includes(run.state) && <button onClick={() => void apiPost(`/planning/runs/${runId}/cancel`, {}).catch(() => setError("取消未确认，请查证后重试。"))}>取消全部子任务</button>}<p className="muted">样本归档前不估造完成比例；Lite 结果不用于正式发布门禁。</p></> : <p>加载中…</p>}</section>;

}

export function PlannedRuns() {
  const rows = useOptions<{ id: string; state: string; created_at: string }>("/planning/runs");
  const states: Record<string, string> = { pending: "等待中", queued: "排队中", running: "执行中", completed: "已完成", failed: "失败", cancelled: "已取消", cancelling: "取消中" };
  return <section className="card planned-runs">
    <div className="planned-runs-header"><div><h2>编排测评任务</h2><p>查看规划提交的测评记录与各数据集执行结果。</p></div><button disabled={rows.loading} onClick={rows.retry}>刷新任务</button></div>
    {rows.error && <p role="alert">{rows.error}</p>}
    {rows.loading ? <p role="status" className="planned-runs-empty">正在加载任务…</p> : !rows.rows.length ? <div className="planned-runs-empty">暂无编排任务，<a href="#console">前往控制台创建测评</a></div> : <div className="table-wrap"><table><thead><tr><th>任务 ID</th><th>执行状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody>{rows.rows.map(r => <tr key={r.id}>
      <td><a className="planned-run-id" href={`#planned-run/${r.id}`}>{r.id}</a></td>
      <td><span className={`planned-state state-${r.state}`}>{states[r.state] || r.state}</span></td>
      <td><time dateTime={r.created_at}>{new Date(r.created_at).toLocaleString()}</time></td>
      <td><a className="planned-run-detail" href={`#planned-run/${r.id}`}>查看详情</a></td>
    </tr>)}</tbody></table></div>}
    <div className="planned-runs-footer"><span>当前页 {rows.rows.length} 条 · 每页最多 50 条</span><div className="inline"><button disabled={rows.loading || !rows.offset} onClick={() => rows.setOffset(Math.max(0, rows.offset - 50))}>上一页</button><button disabled={rows.loading || rows.next == null} onClick={() => rows.setOffset(rows.next!)}>下一页</button></div></div>
  </section>;
}
