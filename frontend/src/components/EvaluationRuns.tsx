import { RunMonitor } from "./RunMonitor";
import { SearchableSelect } from "./SearchableSelect";
import { CopyIdentifier } from "./CopyIdentifier";
import { SubmissionReview } from "./SubmissionReview";
import { ManagementDrawer } from "./ManagementDrawer";
import { ListPagination } from "./ListPagination";
import { useEffect, useState } from "react";
import { CheckCircle2, CircleAlert, Layers, Play } from "lucide-react";
import { apiGet, apiGetPage, apiPost } from "../services/api";
import {
  type Config, type Resource, type Target, eligibleConfig, eligibleTarget, runLink,
  useDefaultConfig, useExplorationSubmission, useOptions, withSelected,
} from "../services/evaluation";
import { CaseHistory } from "./CaseHistory";

type Run = { report_id?: string | null; id: string; state: string; remote_model?: string; resource_kind?: string; created_at: string };
type Snapshot = { sha256: string; config_version_id: string; config_sha256: string; target_id: string; remote_model: string; resource_kind: string; sample_count: number; parameters: Record<string, unknown>; manifest_id: string; manifest_sha256: string; tool?: Record<string, unknown>; profile_id?: string; max_model_requests?: number; execution_origin: string };
type Detail = Run & { updated_at?: string; cancel_requested_at?: string; events?: { seq: number; type: string; created_at: string }[]; run_mode: string; gate_status: string; snapshot: Snapshot | null; execution_unresolved: boolean; cleanup: { state: string; attempt_count: number; last_error_code: string | null }; report: null | { id: string; validity: string; summary: Record<string, unknown> } };
const terminal = (state: string) => ["completed", "failed", "cancelled"].includes(state);
const resourceLabel = (kind?: string) => kind === "aisbench_monitor" ? "远程 AISBench" : kind === "customer_ssh" ? "客户 SSH 服务器" : "本机 Docker";
const time = (value?: string) => value && !Number.isNaN(Date.parse(value)) ? new Date(value).toLocaleString("zh-CN") : "暂无记录";
const stateLabel = (value: string) => ({ completed: "已完成", failed: "执行失败", cancelled: "已取消" }[value] || "执行中");

export function EvaluationRuns({ runId = "", initialTarget = "", allowSubmit = true }: { runId?: string; initialTarget?: string; allowSubmit?: boolean }) {
  const configs = useOptions<Config>("/test-configs");
  const targets = useOptions<Target>("/target-bindings");
  const docker = useOptions<Resource>("/resources");
  const ssh = useOptions<Resource>("/ssh-resources");
  const monitored = useOptions<Resource>("/aisbench-monitored/resources");
  const [resourceKind, setResourceKind] = useState("docker_local");
  const resources = resourceKind === "aisbench_monitor" ? monitored : resourceKind === "customer_ssh" ? ssh : docker;
  const [target, setTarget] = useState<Target | null>(null);
  const [resource, setResource] = useState<Resource | null>(null);
  const { config, setConfig, loadingDefault, defaultError, setDefaultError, reloadDefault } = useDefaultConfig(target);
  const { busy, setBusy, message, setMessage, submit } = useExplorationSubmission();
  const [runs, setRuns] = useState<Run[]>([]);
  const [offset, setOffset] = useState(0);
  const [pageSize,setPageSize] = useState(5);
  const [next, setNext] = useState<number | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState("");
  const [detailTab, setDetailTab] = useState("执行进度");
  const [datasetOpen, setDatasetOpen] = useState(false);
  const [caseOpen, setCaseOpen] = useState(false);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [lastRefresh, setLastRefresh] = useState(0);
  const [detailError, setDetailError] = useState("");
  const [statusFilter, setStatusFilter] = useState("全部状态");
  const [resourceFilter, setResourceFilter] = useState("全部资源");
  const [query, setQuery] = useState("");

  useEffect(() => {
    if (!initialTarget) return;
    const controller = new AbortController();
    setTarget(null); setConfig(null); setMessage("正在加载已保存服务…");
    apiGet<Target[]>(`/target-bindings?target_id=${encodeURIComponent(initialTarget)}`, controller.signal)
      .then((rows) => {
        if (controller.signal.aborted) return;
        if (!rows[0] || !eligibleTarget(rows[0])) { setMessage("服务不存在或不适用于当前GPQA探索路径。"); return; }
        setTarget(rows[0]); setMessage("已加载保存的服务，请确认配置和资源后提交。");
      }).catch(() => { if (!controller.signal.aborted) setMessage("服务读取失败，请返回服务配置重试。"); });
    return () => controller.abort();
  }, [initialTarget]);

  useEffect(() => {
    const controller = new AbortController();
    let refreshing = false;
    setRuns([]); setNext(null); setListLoading(true);
    const refresh = async () => {
      if (refreshing || document.hidden) return;
      refreshing = true;
      try {
        const page = await apiGetPage<Run[]>(`/runs?offset=${offset}&limit=${pageSize}`, controller.signal);
        if (!controller.signal.aborted) { setRuns(page.data); setNext(page.next_offset); setListError(""); setListLoading(false); }
      } catch { if (!controller.signal.aborted) { setListError("任务列表查询失败，恢复连接后自动重试；不能据此判断没有任务。"); setListLoading(false); } }
      finally { refreshing = false; }
    };
    void refresh(); const timer = setInterval(() => void refresh(), 5000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [offset, runId, pageSize]);

  useEffect(() => {
    const controller = new AbortController();
    let refreshing = false;
    setDetail(null); setDetailError("");
    if (!runId) return () => controller.abort();
    const refresh = async () => {
      if (refreshing || document.hidden) return;
      refreshing = true;
      try {
        const row = await apiGet<Detail>(`/runs/${encodeURIComponent(runId)}`, controller.signal);
        if (!controller.signal.aborted) { setDetail(row); setLastRefresh(Date.now()); setDetailError(""); }
      } catch { if (!controller.signal.aborted) setDetailError("任务不存在或查询失败；已显示的状态可能过期，请勿据此重新提交。"); }
      finally { refreshing = false; }
    };
    void refresh(); const timer = setInterval(() => void refresh(), 2000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [runId]);

  const action = async (kind: "cancel" | "cleanup") => {
    setBusy(true);
    try { await apiPost(`/runs/${runId}/${kind}`); setMessage(kind === "cleanup" ? "清理意图已保存；后台核验归档后重试，不会重新测评。" : "取消意图已保存；等待当前请求返回后停止后续样本，已产生的用量不会撤销。"); }
    catch { setMessage("操作未确认，请查询任务状态后重试。"); }
    finally { setBusy(false); }
  };
  const configRows = withSelected(configs.rows.filter(eligibleConfig), config);
  const targetRows = withSelected(targets.rows.filter(eligibleTarget), target);
  const resourceRows = withSelected(resources.rows.filter((row) => resourceKind !== "customer_ssh" || row.native_tool_root), resource);
  const filteredRuns = runs.filter((row) => {
    const statusMatches = statusFilter === "全部状态" || (statusFilter === "执行中" ? !terminal(row.state) : row.state === statusFilter);
    const resourceMatches = resourceFilter === "全部资源" || row.resource_kind === resourceFilter;
    const queryMatches = `${row.id} ${row.remote_model || ""}`.toLowerCase().includes(query.toLowerCase());
    return statusMatches && resourceMatches && queryMatches;
  });

  return (
    <div className="stack evaluation-runs">
      {allowSubmit && (
        <section className="card stack">
          <div><h2>新建探索测评</h2><p className="muted">GPQA Lite 使用固定原生 AISBench 路径，每次最多 5 个样本、每样本最多 64 输出 tokens，无自动重测。</p></div>
          <p><a href="#console">登记模型、服务与资源</a> · <a href="#config">维护配置、清单与 Profile</a></p>
          <label>探索模型服务 <SearchableSelect aria-label="探索模型服务" value={target?.id || ""} disabled={busy || targets.loading} onChange={(event) => setTarget(targetRows.find((row) => row.id === event.target.value) || null)}><option value="">请选择官方DeepSeek目标</option>{targetRows.map((row) => <option key={row.id} value={row.id}>{row.model_key} / {row.model_version} · {row.remote_model} · {row.id.slice(0, 8)}</option>)}</SearchableSelect></label>
          <label>探索配置 <SearchableSelect aria-label="探索配置" value={config?.id || ""} disabled={busy || loadingDefault} onChange={(event) => { setConfig(configRows.find((row) => row.id === event.target.value) || null); setDefaultError(""); }}><option value="">请选择已绑定Profile的Lite配置</option>{configRows.map((row) => <option key={row.id} value={row.id}>{row.display_name} · {row.version_label}</option>)}</SearchableSelect></label>
          {loadingDefault && <p role="status">正在读取模型默认配置…</p>}{defaultError && <p role="status">{defaultError}</p>}
          <button disabled={busy || !target || loadingDefault} onClick={reloadDefault}>重新加载模型默认配置</button>
          <label>探索资源接入方式<select aria-label="探索资源接入方式" value={resourceKind} disabled={busy} onChange={(event) => { setResourceKind(event.target.value); setResource(null); }}><option value="docker_local">本机Docker</option><option value="customer_ssh">客户SSH服务器</option><option value="aisbench_monitor">远程 AISBench（运行监控）</option></select></label>
          <label>探索执行资源 <SearchableSelect aria-label="探索执行资源" value={resource?.id || ""} disabled={busy || resources.loading} onChange={(event) => setResource(resourceRows.find((row) => row.id === event.target.value) || null)}><option value="">请选择资源</option>{resourceRows.map((row) => <option key={row.id} value={row.id}>{row.binding_key}</option>)}</SearchableSelect></label>
          {[configs, targets, resources].some((source) => source.error) && <p role="alert">选项加载失败。<button onClick={() => { configs.retry(); targets.retry(); resources.retry(); }}>重试选项加载</button></p>}
          <p className="muted">配置须为 GPQA 单数据集、Lite、并发 1、temperature=0、top_p=1。每页 20 条，翻页保留已选项，提交时后台再次校验。</p>
          <SubmissionReview config={loadingDefault ? null : config} target={target} resource={resource} resourceKind={resourceKind} busy={busy} submit={() => void submit(config, target, resource, resourceKind)} />
        </section>
      )}
      {message && <p role="status">{message}</p>}{listError && <p role="alert">{listError}</p>}

      {!runId && !initialTarget && (
        <>
          <div className="stats task-stats" aria-label="当前页任务统计">
            <section className="card"><div className="stat-icon"><Layers /></div><div>当前页任务<strong>{runs.length}</strong></div></section>
            <section className="card"><div className="stat-icon tone-1"><Play /></div><div>当前页执行中<strong>{runs.filter((row) => !terminal(row.state)).length}</strong></div></section>
            <section className="card"><div className="stat-icon tone-2"><CheckCircle2 /></div><div>当前页已完成<strong>{runs.filter((row) => row.state === "completed").length}</strong></div></section>
            <section className="card"><div className="stat-icon tone-3"><CircleAlert /></div><div>当前页失败 / 取消<strong>{runs.filter((row) => ["failed", "cancelled"].includes(row.state)).length}</strong></div></section>
          </div>
          <section className="card stack">
            <div className="spread"><div><h2>测评任务</h2><p className="muted">统计与筛选范围为当前页。</p></div><a className="link" href="#console">发起探索测评</a></div>
            <div className="filters">
              <label className="select-field"><span>状态</span><select aria-label="任务状态筛选" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option>全部状态</option><option>执行中</option><option value="completed">已完成</option><option value="failed">执行失败</option><option value="cancelled">已取消</option></select></label>
              <label className="select-field"><span>资源</span><select aria-label="任务资源筛选" value={resourceFilter} onChange={(event) => setResourceFilter(event.target.value)}><option value="全部资源">全部资源</option><option value="docker_local">本机 Docker</option><option value="customer_ssh">客户 SSH 服务器</option><option value="aisbench_monitor">远程 AISBench</option></select></label>
              <label className="search"><input aria-label="搜索任务" placeholder="搜索任务 ID 或模型" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
            </div>

            <div className="table-wrap"><table><thead><tr><th>任务</th><th>模型</th><th>运行方式</th><th>资源</th><th>状态</th><th>报告</th><th>创建时间</th><th>操作</th></tr></thead><tbody>{filteredRuns.map((row) => <tr key={row.id}><td><a className="link strong" href={runLink(row.id)} title={row.id}>任务 {row.id.slice(0, 8)}</a></td><td>{row.remote_model || "—"}</td><td>GPQA Lite · 探索</td><td>{resourceLabel(row.resource_kind)}</td><td><span className={`case-status ${row.state === "completed" ? "passed" : ["failed", "cancelled"].includes(row.state) ? "error" : "running"}`}>{{ completed: "已完成", failed: "执行失败", cancelled: "已取消" }[row.state] || "执行中"}</span></td><td>{row.report_id ? <a className="link" href={`#report/${row.report_id}`}>已生成</a> : "未生成"}</td><td>{time(row.created_at)}</td><td><a className="link" href={runLink(row.id)}>查看</a></td></tr>)}</tbody></table>{!filteredRuns.length && <div className="empty">{listLoading ? "任务加载中…" : listError ? "任务查询失败" : "当前页没有符合条件的任务"}</div>}</div>
            <ListPagination label="任务" offset={offset} nextOffset={next} pageSize={pageSize} onOffsetChange={setOffset} onPageSizeChange={setPageSize} disabled={busy} />
          </section>
        </>
      )}

      {runId && <a className="link back" href="#tasks">返回任务列表</a>}
      {detailError && <p role="alert">{detailError}</p>}
      {detail && (
        <article className="card stack execution-detail restored-detail" data-testid="evaluation-detail">
          <div className="spread"><div className="detail-title"><h2 title={detail.id}>任务 {detail.id.slice(0, 8)}</h2><span className="badge" data-testid="evaluation-state">{stateLabel(detail.state)}</span><span className="muted">探索测评</span></div>
            <div className="inline">{!terminal(detail.state) && <button disabled={busy} onClick={() => void action("cancel")}>取消测评</button>}{detail.report && <a className="primary button" href={`#report/${detail.report.id}`}>查看报告</a>}</div></div>
          <div className="execution-meta">
            <span>模型 <b>{detail.snapshot?.remote_model || "—"}</b></span>
            <span>运行方式 <b>串行 · 探索</b></span>
            <span>创建时间 <b>{time(detail.created_at)}</b></span><span>状态更新时间 <b>{time(detail.updated_at)}</b></span><span>最近查询 <b>{lastRefresh ? new Date(lastRefresh).toLocaleTimeString("zh-CN") : "尚未刷新"}</b></span>
            <span>资源 <b>{resourceLabel(detail.snapshot?.resource_kind)}</b></span>
            <span>发布门禁 <b>不适用</b></span>
          </div>
          <div className="tabs">{["执行进度", "配置快照"].map((name) => <button key={name} className={detailTab === name ? "active" : ""} onClick={() => setDetailTab(name)}>{name}</button>)}</div>
          {detail.execution_unresolved && <p className="notice" role="alert">执行状态待查证，资源占用和已有记录已保留，请勿重复提交。</p>}
          {detailTab === "配置快照" ? <section data-testid="run-snapshot"><h3>本次配置快照</h3><dl className="detail-properties"><div><dt>执行参数</dt><dd>{detail.snapshot?.parameters ? Object.entries(detail.snapshot.parameters).map(([key, value]) => `${({output:"回答长度", concurrency:"并发数", temperature:"随机性", top_p:"候选范围", scope:"范围"} as Record<string,string>)[key] || key}：${String(value)}`).join("；") : "暂无信息"}</dd></div><div><dt>样本清单</dt><dd>{detail.snapshot?.manifest_id || "暂无信息"}</dd></div><div><dt>工具版本</dt><dd>{String(detail.snapshot?.tool?.revision || "暂无信息")}</dd></div></dl><p className="muted">本次提交时冻结，后续配置修改不会影响历史任务。</p><dl className="detail-properties"><div><dt>模型</dt><dd>{detail.snapshot?.remote_model || "—"}</dd></div><div><dt>样本数</dt><dd>{detail.snapshot?.sample_count ?? "—"}</dd></div><div><dt>配置版本</dt><dd>{detail.snapshot?.config_version_id || "—"}</dd></div></dl><ManagementDrawer title="查看完整快照与校验值"><pre className="snapshot">{JSON.stringify(detail.snapshot, null, 2)}</pre></ManagementDrawer></section> : <>
            <div className="spread">{datasetOpen ? <button className="link" onClick={() => { setDatasetOpen(false); setCaseOpen(false); }}>返回任务总览</button> : <h3>任务总览</h3>}<span className="muted">{datasetOpen ? "GPQA · Lite" : "1 个数据集"}</span></div>
            <section className="detail-progress"><div className="spread"><b>{datasetOpen ? "GPQA 执行状态" : "整体执行状态"}</b><span>{stateLabel(detail.state)}</span></div>{detailError && <p className="notice" role="alert">当前展示为上次查询结果，状态可能过期；恢复连接后自动刷新。</p>}{detail.state === "failed" && <p role="alert">执行失败。请查看 Case 的错误类别和证据，确认原因后再决定后续操作。<button className="link" onClick={() => { setDatasetOpen(true); setCaseOpen(true); }}>定位失败记录</button></p>}<p>当前阶段：{({ accepted: "已接收，等待调度", queued: "排队中", preparing: "准备执行环境", running: "测评执行中", aggregating: "收集结果与生成报告", completed: "执行结束", cancelling: "取消确认中，仍可能保留资源占用", cancelled: "取消已确认", failed: "执行失败" } as Record<string, string>)[detail.state] || "状态待确认"}</p>{detail.cancel_requested_at && <p>取消请求：{time(detail.cancel_requested_at)}；以终止确认及清理状态为准。</p>}<p className="muted">{detail.report ? "报告已归档，当前仅展示历史执行统计；成绩与证据完整性请查看报告。" : "报告尚未归档，暂不展示未经确认的样本进度。"}</p></section>
            <RunMonitor runId={detail.id} terminal={terminal(detail.state)} />
            <div className="task-overview-stats">{[["计划样本", detail.snapshot?.sample_count], ["执行成功", detail.report?.summary.successful_samples], ["执行失败", detail.report?.summary.failed_samples], ["缺测样本", detail.report?.summary.missing_samples], ["判分通过", "—"], ["判分未通过", "—"]].map(([label, value]) => <div className="case-stat" key={String(label)}><span>{String(label)}</span><strong>{value === undefined ? "—" : String(value)}</strong></div>)}</div>
            {!datasetOpen ? <div className="table-wrap"><table><thead><tr><th>数据集</th><th>状态</th><th>范围 / 样本数</th><th>报告状态</th><th>操作</th></tr></thead><tbody><tr><td>GPQA</td><td>{stateLabel(detail.state)}</td><td>Lite · {detail.snapshot?.sample_count ?? "—"}</td><td>{detail.report ? "已归档，待核验证据" : "待生成"}</td><td><button className="link" onClick={() => setDatasetOpen(true)}>查看明细</button></td></tr></tbody></table></div> : <>
              <div className="tabs">{[false, true].map((value) => <button key={String(value)} className={caseOpen === value ? "active" : ""} onClick={() => setCaseOpen(value)}>{value ? "Case 明细" : "执行概况"}</button>)}</div>
              {caseOpen ? <CaseHistory key={detail.id} initialRun={detail.id} /> : <section className="detail-progress"><h3>执行概况</h3><p>执行状态：{stateLabel(detail.state)}。执行成功与判分通过分别记录。</p><button className="link" onClick={() => setCaseOpen(true)}>查看 Case 明细与日志</button></section>}
            </>}
            <div className="detail-bottom-meta"><span data-testid="cleanup-state">资源清理：{({ not_required: "无需清理", not_scheduled: "尚未安排", pending: "待清理", running: "清理中", failed: "待恢复，保留占用", cleaned: "已确认" } as Record<string, string>)[detail.cleanup.state] || "待确认"}</span>{["failed", "not_scheduled"].includes(detail.cleanup.state) && detail.report && <button disabled={busy} onClick={() => void action("cleanup")}>重试资源清理</button>}<ManagementDrawer title="任务标识与归档记录"><h3>状态记录</h3><ol>{detail.events?.map((event) => <li key={event.seq}>{time(event.created_at)} · {({ accepted: "已接收", queued: "排队", preparing: "准备环境", running: "开始执行", aggregating: "汇总结果", completed: "执行完成", failed: "执行失败", cancelled: "取消确认", cancelling: "取消中", execution_unresolved: "结果待查证", execution_cleaned: "清理确认", execution_cleanup_pending: "等待清理" } as Record<string, string>)[event.type] || "状态变更"}</li>)}</ol><p>运行 ID：{detail.id}</p><CopyIdentifier value={detail.id} /><p>清理尝试：{detail.cleanup.attempt_count} 次</p><p data-testid="evaluation-validity">历史报告状态：{detail.report?.validity || "未归档"}</p><p>历史摘要不代表当前证据完整，成绩以报告核验结果为准。</p><pre className="snapshot" data-testid="evaluation-summary">{JSON.stringify(detail.report?.summary || {}, null, 2)}</pre></ManagementDrawer></div>
          </>}
        </article>
      )}
    </div>
  );
}
