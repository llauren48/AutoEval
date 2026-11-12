import { useEffect, useState } from "react";
import { apiGet, apiGetPage } from "../services/api";

type Monitor = {
  collection_status: string;
  stale: boolean;
  source?: string;
  task_name?: string;
  run_id?: string;
  attempt?: number;
  process_id?: number;
  tool_status?: string;
  finish_count?: number | null;
  total_count?: number | null;
  observed_at?: string;
  snapshot_kind?: string;
  progress_description?: string;
  progress_text?: string;
  time_cost?: string;
  tool_start_time?: number;
  diagnostics?: string[];
  log_available?: boolean;
  job_dir_available?: boolean;
  log?: { endpoint: string; sha256: string } | null;
};
type Snapshot = { id: string; kind: string; sha256: string; created_at: string };
type ArchivedLog = { content: string; sha256: string; source: string };

const KIND_LABEL: Record<string, string> = {
  final: "终态已归档",
  last_observed: "仅最后观测（非终态）",
  live: "实时观测",
};
const STATUS_LABEL: Record<string, string> = {
  finish: "已完成",
  finished: "已完成",
  inferencing: "推理中",
  processing: "处理中",
  pending: "等待中",
  started: "执行中",
  succeeded: "执行成功",
  failed: "执行失败",
};
const PAGE = 20;
const stamp = (value?: number | string | null) => {
  if (value === null || value === undefined || value === "") return "—";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN");
};

function MonitorProgress({ value, error = false, stage, terminal = false }: { value: Monitor | null; error?: boolean; stage?: string; terminal?: boolean }) {
  const total = value?.total_count;
  const done = value?.finish_count;
  const known = typeof total === "number" && Number.isInteger(total) && total > 0
    && typeof done === "number" && Number.isInteger(done) && done >= 0 && done <= total;
  const percent = known ? Math.round(done / total * 100) : undefined;
  const stale = Boolean(value?.stale || error);
  const kind = value?.snapshot_kind || "live";
  const waiting = terminal ? "未记录样本进度" : stale ? "等待恢复采集" : "等待样本计数";
  return <div className={`run-progress ${kind}${stale ? " stale" : ""}`} data-testid="run-progress">
    <div className="run-progress-head"><span>样本执行进度</span><b>{known ? `${done} / ${total} · ${percent}%` : waiting}</b></div>
    <div className={`run-progress-track${known ? "" : " unknown"}`} role="progressbar"
      aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100} aria-label="工具执行进度"
      aria-valuetext={known ? `已完成 ${done} / ${total} 个样本，${percent}%${stale ? "，数据已过期" : ""}` : waiting}>
      <i style={known ? { width: `${percent}%` } : undefined} />
    </div>
    <small>{stage && <>当前阶段：{stage} · </>}
      {stale ? "数据已过期，保留最后观测值" : known ? "进度来自工具监控，执行完成不代表评分通过" : terminal ? "任务已结束，未记录有效样本计数" : "样本计数尚未返回，当前阶段与耗时持续更新"}
      {value?.snapshot_kind === "final" ? " · 终态已归档" : value?.snapshot_kind === "last_observed" ? " · 仅最后观测（非终态）" : ""}
    </small>
  </div>;
}

type ProgressEvent = { type: string; created_at: string };
const PHASES = ["提交与排队", "环境准备", "测评执行", "结果归档", "任务完成"];
const PHASE_INDEX: Record<string, number> = { accepted: 0, queued: 0, preparing: 1, running: 2, aggregating: 3, completed: 4 };
const duration = (milliseconds: number) => {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  const hours = Math.floor(seconds / 3600);
  return `${hours ? `${hours}小时 ` : ""}${Math.floor(seconds % 3600 / 60)}分 ${String(seconds % 60).padStart(2, "0")}秒`;
};

export function RunProgress({ runId, terminal, stage, state, createdAt, events = [] }: {
  runId: string; terminal: boolean; stage: string; state: string; createdAt?: string; events?: ProgressEvent[];
}) {
  const [value, setValue] = useState<Monitor | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [now, setNow] = useState(Date.now);
  useEffect(() => { setValue(null); setUnavailable(false); }, [runId]);
  useEffect(() => {
    const tick = () => { if (!document.hidden) setNow(Date.now()); };
    tick();
    if (terminal) return;
    const timer = setInterval(tick, 1000);
    document.addEventListener("visibilitychange", tick);
    return () => { clearInterval(timer); document.removeEventListener("visibilitychange", tick); };
  }, [runId, terminal]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    setUnavailable(false);
    const refresh = async () => {
      if (!document.hidden) {
        try {
          const row = await apiGet<Monitor>(`/runs/${runId}/monitor`, controller.signal);
          if (!controller.signal.aborted) { setValue(row); setUnavailable(false); }
        } catch { if (!controller.signal.aborted) setUnavailable(true); }
      }
      if (!controller.signal.aborted && !terminal) timer = setTimeout(refresh, 2000);
    };
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [runId, terminal]);

  const start = createdAt ? Date.parse(createdAt) : NaN;
  const endEvent = events.filter(event => event.type === state).at(-1);
  const end = terminal ? endEvent ? Date.parse(endEvent.created_at) : NaN : now;
  const elapsed = Number.isFinite(start) && Number.isFinite(end) && end >= start ? duration(end - start) : "暂无时间记录";
  const stopped = state === "failed" || state === "cancelled" || state === "cancelling";
  const observedPhases = events.map(event => PHASE_INDEX[event.type]).filter(index => index !== undefined);
  const current = PHASE_INDEX[state] ?? (observedPhases.length ? Math.max(...observedPhases) : 0);
  return <section className={`task-progress-panel${stopped ? " interrupted" : ""}`} aria-label="任务进度与耗时">
    <div className="task-progress-summary"><div><span className="muted">当前阶段</span><strong>{stage}</strong></div>
      <div className="task-elapsed"><span className="muted">{terminal ? "总耗时" : "当前耗时"}</span><strong data-testid="run-elapsed">{elapsed}</strong></div>
    </div>
    <ol className="task-phase-steps" aria-label="任务阶段">
      {PHASES.map((label, index) => <li key={label} className={index < current || state === "completed" ? "done" : index === current ? stopped ? "interrupted" : "current" : "pending"} aria-current={index === current ? "step" : undefined}>
        <span className="task-phase-dot" aria-hidden="true">{index < current || state === "completed" ? "✓" : index === current && !terminal && !stopped ? <span className="phase-spinner" /> : stopped && index === current ? "!" : index + 1}</span><span>{label}</span>
      </li>)}
    </ol>
    {!terminal && <p className="task-wait-hint">{state === "accepted" || state === "queued" ? "提交已接收，正在等待执行资源" : state === "preparing" ? "正在校验环境并启动工具，阶段变化会自动更新" : state === "aggregating" ? "正在收集结果、校验证据并生成报告" : state === "cancelling" ? "正在确认远程作业停止，资源仍保留占用" : "工具正在执行；样本计数返回后自动显示完成比例"}</p>}
    <MonitorProgress value={value} error={unavailable} terminal={terminal} />
    <small className="muted">任务耗时包含排队、环境准备、测评与归档；样本进度和评分结论分别记录。</small>
  </section>;
}

export function RunMonitor({ runId, terminal }: { runId: string; terminal: boolean }) {
  const [tab, setTab] = useState<"overview" | "snapshots" | "log">("overview");
  const [value, setValue] = useState<Monitor | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [snapshotOffset, setSnapshotOffset] = useState(0);
  const [snapshotMessage, setSnapshotMessage] = useState("");
  const [selected, setSelected] = useState("");
  const [evidence, setEvidence] = useState<unknown>(null);

  const [log, setLog] = useState<ArchivedLog | null>(null);
  const [logMessage, setLogMessage] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    setValue(null); setError(""); setLoading(true); setTab("overview");
    setSnapshots([]); setNext(null); setSnapshotOffset(0); setSelected(""); setEvidence(null);
    setSnapshotMessage(""); setLog(null); setLogMessage("");
    const refresh = async () => {
      if (!document.hidden) {
        try {
          const row = await apiGet<Monitor>(`/runs/${runId}/monitor`, controller.signal);
          if (!controller.signal.aborted) { setValue(row); setError(""); }
        } catch {
          if (!controller.signal.aborted) setError("监控采集暂不可用，保留最后观测值");
        } finally {
          if (!controller.signal.aborted) setLoading(false);
        }
      }
      if (!controller.signal.aborted && !terminal) timer = setTimeout(refresh, 2000);
    };
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [runId, terminal]);

  const loadSnapshots = async (offset: number) => {
    setSnapshotMessage("正在读取归档快照…");
    try {
      const page = await apiGetPage<Snapshot[]>(`/runs/${runId}/monitor/snapshots?limit=${PAGE}&offset=${offset}`);
      setSnapshots(page.data); setNext(page.next_offset); setSnapshotOffset(offset);
      setSnapshotMessage(page.data.length ? "" : "本次运行暂无归档快照。");
    } catch {
      setSnapshots([]); setNext(null); setSnapshotMessage("监控归档目录暂不可用。");
    }
  };
  const openSnapshot = async (id: string) => {
    setSelected(id); setEvidence(null);
    try { setEvidence(await apiGet(`/runs/${runId}/monitor/snapshots/${id}`)); }
    catch { setEvidence({ message: "证据尚未归档或校验未通过" }); }
  };
  const loadLog = async () => {
    setLogMessage("正在读取归档日志…");
    try { setLog(await apiGet<ArchivedLog>(`/runs/${runId}/monitor/log`)); setLogMessage(""); }
    catch {
      setLog(null);
      setLogMessage("本次运行未归档执行日志，或日志校验未通过。");
    }
  };

  if (loading && !value) return <p className="muted" role="status">正在读取工具监控…</p>;
  if (!value) return <p className="muted" role="status">{error || "工具监控暂不可用。"}</p>;
  if (value.collection_status === "not_collected") return <p className="muted" role="status">{error || "本次运行未采集工具监控。"}</p>;

  const kind = value.snapshot_kind || "live";
  const kindTone = value.stale || error ? "orange" : kind === "final" ? "green" : kind === "live" ? "blue" : "orange";
  const evidenceRow = typeof evidence === "object" && evidence !== null ? (evidence as Record<string, unknown>) : null;
  const projection = evidenceRow && typeof evidenceRow.projection === "object" ? evidenceRow.projection : evidence;
  const raw = evidenceRow?.raw;

  return (
    <section className="card stack monitor-panel" data-testid="run-monitor">
      <div className="spread">
        <h3>工具运行监控</h3>
        <div className="inline">
          <span className={`badge ${kindTone}`}>{KIND_LABEL[kind] || kind}</span>
          <span className="muted">{STATUS_LABEL[value.tool_status || ""] || value.tool_status || "状态未知"}</span>
        </div>
      </div>
      <MonitorProgress value={value} error={Boolean(error)} terminal={terminal} />
      {(value.stale || error) && (
        <p className="notice" role="status">数据已过期 / 不可达，以下为最后观测值；采集失败不会触发测评重启。</p>
      )}

      <div className="tabs monitor-tabs">
        {([["overview", "概览"], ["snapshots", "归档快照"], ["log", "日志"]] as const).map(([key, label]) => (
          <button
            key={key}
            className={tab === key ? "active" : ""}
            onClick={() => {
              setTab(key);
              if (key === "snapshots" && !snapshots.length && snapshotOffset === 0 && !snapshotMessage.startsWith("正在")) void loadSnapshots(0);
              if (key === "log") void loadLog();
            }}
          >{label}</button>
        ))}
      </div>

      {tab === "overview" && (
        <>
          <dl className="detail-properties">
            <div><dt>任务名</dt><dd>{value.task_name || "—"}</dd></div>
            <div><dt>运行 ID</dt><dd>{value.run_id || runId}</dd></div>
            <div><dt>执行尝试</dt><dd>{value.attempt ?? "—"}</dd></div>
            <div><dt>工具进程</dt><dd>{value.process_id ?? "—"}</dd></div>
            <div><dt>工具开始时间</dt><dd>{stamp(value.tool_start_time)}</dd></div>
            <div><dt>工具耗时</dt><dd>{value.time_cost || "—"}</dd></div>
            <div><dt>采集时间</dt><dd>{stamp(value.observed_at)}</dd></div>
            <div><dt>数据来源</dt><dd>{value.source || "—"}</dd></div>
            <div><dt>采集状态</dt><dd>{value.stale ? "已过期（采集不可达）" : "正常"}</dd></div>
            <div><dt>归档能力</dt><dd>{value.log_available ? "日志已归档" : "日志未归档"} · {value.job_dir_available ? "作业目录可用" : "作业目录不可用"}</dd></div>
          </dl>
          {(value.diagnostics?.length ?? 0) > 0 && (
            <p className="muted">诊断：{value.diagnostics?.join("、")}</p>
          )}
          <details>
            <summary>原始投影数据</summary>
            <pre className="snapshot" data-testid="monitor-raw">{JSON.stringify(value, null, 2)}</pre>
          </details>
          <p className="muted">工具状态来自 AISBench 监控对象；平台状态与评分结论请在「执行进度」和报告中查看。</p>
        </>
      )}

      {tab === "snapshots" && (
        <>
          {snapshotMessage && <p role="status" className="muted">{snapshotMessage}</p>}
          {snapshots.length > 0 && (
            <ol className="monitor-list" data-testid="monitor-snapshots">
              {snapshots.map((row) => (
                <li key={row.id}>
                  <button
                    type="button"
                    className={`link ${selected === row.id ? "active" : ""}`}
                    onClick={() => void openSnapshot(row.id)}
                  >
                    <span className={`badge ${row.kind === "final" ? "green" : row.kind === "live" ? "blue" : "orange"}`}>{row.kind}</span>
                    <span>{stamp(row.created_at)}</span>
                    <span className="mono">{row.sha256.slice(0, 12)}</span>
                  </button>
                </li>
              ))}
            </ol>
          )}
          {snapshots.length > 0 && (
            <div className="pagination">
              <button disabled={snapshotOffset === 0} onClick={() => void loadSnapshots(Math.max(0, snapshotOffset - PAGE))}>上一页</button>
              <span>第 {snapshotOffset / PAGE + 1} 页</span>
              <button disabled={next === null} onClick={() => next !== null && void loadSnapshots(next)}>下一页</button>
            </div>
          )}
          {evidence !== null && (
            <div className="monitor-evidence">
              <h4>快照内容 · 已通过完整性校验</h4>
              <details open>
                <summary>投影字段</summary>
                <pre className="snapshot" data-testid="monitor-evidence">{JSON.stringify(projection, null, 2)}</pre>
              </details>
              {raw !== undefined && (
                <details>
                  <summary>原始字段（脱敏）</summary>
                  <pre className="snapshot">{JSON.stringify(raw, null, 2)}</pre>
                </details>
              )}
            </div>
          )}
        </>
      )}

      {tab === "log" && (
        <>
          {logMessage && <p role="status" className="muted">{logMessage}</p>}
          {log && (
            <>
              <p className="muted">来源：归档作业日志 · SHA-256 {log.sha256.slice(0, 12)} · 读取时已核验完整性。</p>
              <pre className="snapshot monitor-log" data-testid="monitor-log">{log.content}</pre>
            </>
          )}
        </>
      )}
    </section>
  );
}
