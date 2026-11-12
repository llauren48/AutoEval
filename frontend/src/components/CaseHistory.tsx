import { createPortal } from "react-dom";
import { useEffect, useRef, useState } from "react";
import { apiGet, apiGetPage } from "../services/api";
import { UnifiedOutputSummary } from "./UnifiedOutputSummary";

type ToolMetadata = {
  score?: number | null;
  tool_started_at: string | null;
  tool_finished_at: string | null;
  duration_sec: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  diagnostics: string[];
  status: string;
};
type Attempt = {
  id: string;
  dataset_key: string;
  sample_id: string;
  attempt_no: number;
  state: string;
  error_code: string | null;
  evidence_available: boolean;
  evidence_sha256: string | null;
  created_at: string;
  finished_at: string | null;
  tool_metadata?: ToolMetadata | null;
};
type Query = { run: string; sample: string; state: string; offset: number; size: number };

const time = (value: string | null) => value && !Number.isNaN(Date.parse(value)) ? new Date(value).toLocaleString("zh-CN") : "—";
const STATUS: Record<string, { label: string; tone: string }> = {
  succeeded: { label: "执行成功", tone: "passed" },
  failed: { label: "执行失败", tone: "error" },
  started: { label: "执行中 / 待查证", tone: "running" },
};
const caseScore = (metadata?: ToolMetadata | null) => typeof metadata?.score === "number" && Number.isFinite(metadata.score) ? String(metadata.score) : "未提供评分";
const short = (value: string | null) => (value ? `${value.slice(0, 12)}…` : "—");

export function CaseHistory({ initialRun = "" }: { initialRun?: string }) {
  const evidenceDialog = useRef<HTMLDialogElement>(null);
  const [run, setRun] = useState(initialRun);
  const [stateFilter, setStateFilter] = useState("");
  const [sample, setSample] = useState("");
  const [pageSize, setPageSize] = useState(20);
  const [query, setQuery] = useState<Query | null>(null);
  const [rows, setRows] = useState<Attempt[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [selected, setSelected] = useState<Attempt | null>(null);
  const [evidence, setEvidence] = useState<unknown>(null);
  const [message, setMessage] = useState("");

  useEffect(() => { if (evidence !== null) evidenceDialog.current?.showModal(); }, [evidence]);

  useEffect(() => {
    if (initialRun) {
      setRun(initialRun);
      setQuery({ run: initialRun, sample: "", state: "", offset: 0, size: pageSize });
    }
  }, [initialRun]);

  useEffect(() => {
    if (!query) return;
    const controller = new AbortController();
    setRows([]); setNext(null); setTotal(null); setSelected(null); setEvidence(null); setMessage("正在查询历史…");
    const parts = [`limit=${query.size}`, `offset=${query.offset}`];
    if (query.sample) parts.push(`sample_id=${encodeURIComponent(query.sample)}`);
    if (query.state) parts.push(`state=${encodeURIComponent(query.state)}`);
    apiGetPage<Attempt[]>(`/runs/${encodeURIComponent(query.run)}/case-executions?${parts.join("&")}`, controller.signal)
      .then((page) => {
        setRows(page.data); setNext(page.next_offset); setTotal(page.total ?? null);
        setMessage(
          page.data.length
            ? "筛选已应用到全部记录，评分结论请以报告为准。"
            : query.state || query.sample
              ? "没有符合筛选条件的执行记录。"
              : "该运行暂无 case 执行记录。"
        );
      })
      .catch((error) => {
        if (!controller.signal.aborted) setMessage(error instanceof Error ? error.message : "历史查询失败");
      });
    return () => controller.abort();
  }, [query]);

  const applyState = (value: string) => {
    setStateFilter(value);
    if (!initialRun) return;
    setQuery({ run: initialRun, sample: sample.trim(), state: value, offset: 0, size: pageSize });
  };
  const changeSize = (value: number) => {
    setPageSize(value);
    if (query) setQuery({ ...query, offset: 0, size: value });
  };

  const openEvidence = (row: Attempt) => {
    setSelected(row);
    setEvidence(null);
    const controller = new AbortController();
    apiGet<unknown>(`/case-executions/${row.id}/evidence`, controller.signal)
      .then(setEvidence)
      .catch((error) => {
        if (!controller.signal.aborted) setEvidence({ message: error instanceof Error ? error.message : "证据读取失败" });
      });
  };
  const detail = evidence && typeof evidence === "object" ? (evidence as Record<string, unknown>) : null;
  const meta = selected?.tool_metadata ?? null;
  const page = query ? Math.floor(query.offset / query.size) + 1 : 1;
  const pages = total !== null ? Math.max(1, Math.ceil(total / (query?.size ?? pageSize))) : null;

  const body = (
    <>
      {initialRun && <UnifiedOutputSummary runId={initialRun} compact />}
      <p className="muted">查询每次执行尝试及已校验的原始证据。重试成功仍保留此前失败；执行状态与评分正确性分开。</p>
      <div className="filters case-toolbar">
        {!initialRun && <label>运行ID <input aria-label="回溯运行ID" value={run} onChange={(event) => setRun(event.target.value)} /></label>}
        <label>样本ID（可选） <input aria-label="回溯样本ID" value={sample} onChange={(event) => setSample(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && run.trim()) setQuery({ run: run.trim(), sample: sample.trim(), state: stateFilter, offset: 0, size: pageSize }); }} /></label>
        <label>执行状态 <select aria-label="Case执行状态筛选" value={stateFilter} onChange={(event) => applyState(event.target.value)}><option value="">全部尝试</option><option value="succeeded">执行成功</option><option value="failed">执行失败</option><option value="started">执行中 / 待查证</option></select></label>
        {!initialRun && <button type="button" disabled={!run.trim()} onClick={() => setQuery({ run: run.trim(), sample: sample.trim(), state: stateFilter, offset: 0, size: pageSize })}>查询case历史</button>}
        {initialRun && <button type="button" onClick={() => setQuery({ run: initialRun, sample: sample.trim(), state: stateFilter, offset: 0, size: pageSize })}>筛选</button>}
      </div>
      <p className="muted">模型答错属于评分结果；超时、断网等属于执行异常。得分为工具归档的原始分值，未提供评分不补零。此表保留每次尝试，筛选作用于全部已归档记录。</p>
      <p role="status">{message}</p>
      {rows.length > 0 && (
        <div className="table-wrap" data-testid="case-attempts">
          <table>
            <thead><tr><th>数据集 / 样本</th><th>尝试</th><th>执行状态</th><th>得分</th><th>创建时间</th><th>耗时</th><th>Token（入 / 出）</th><th>证据</th></tr></thead>
            <tbody>{rows.map((row) => {
              const status = STATUS[row.state] ?? { label: "待确认", tone: "pending" };
              return (
                <tr key={row.id}>
                  <td><b>{row.dataset_key}</b><small>{row.sample_id}</small></td>
                  <td>第 {row.attempt_no} 次</td>
                  <td>
                    <span className={`case-status ${status.tone}`}>{status.label}</span>
                    {row.error_code && <small className="case-cell-note">{row.error_code}</small>}
                  </td>
                  <td>{caseScore(row.tool_metadata)}</td>
                  <td>{time(row.created_at)}</td>
                  <td>{row.tool_metadata?.duration_sec != null ? `${row.tool_metadata.duration_sec} 秒` : "—"}</td>
                  <td>{row.tool_metadata ? `${row.tool_metadata.input_tokens ?? "—"} / ${row.tool_metadata.output_tokens ?? "—"}` : "—"}</td>
                  <td>
                    <button className="link" type="button" disabled={!row.evidence_available} onClick={() => openEvidence(row)}>查看第{row.attempt_no}次证据</button>
                    <small>{row.evidence_available ? short(row.evidence_sha256) : "尚未归档"}</small>
                  </td>
                </tr>
              );
            })}</tbody>
          </table>
        </div>
      )}
      {query && (
        <div className="pagination" aria-label="Case 列表分页">
          <span>{total !== null ? `共 ${total} 条 · 第 ${page} / ${pages} 页` : `第 ${page} 页`}</span>
          <label className="pagination-page-size"><span>每页</span><select aria-label="每页条数" value={pageSize} onChange={(event) => changeSize(Number(event.target.value))}>{[20, 50, 100].map((size) => <option key={size} value={size}>{size} 条</option>)}</select></label>
          <button disabled={query.offset === 0} onClick={() => setQuery({ ...query, offset: Math.max(0, query.offset - query.size) })}>上一页</button>
          <button className="primary" aria-current="page">{page}</button>
          <button disabled={next === null} onClick={() => { if (next !== null) setQuery({ ...query, offset: next }); }}>下一页</button>
        </div>
      )}
      {evidence !== null && createPortal(
        <dialog ref={evidenceDialog} className="management-drawer" aria-label="Case 执行证据" onCancel={() => setEvidence(null)}>
          <header className="spread"><h2>第 {selected?.attempt_no ?? "—"} 次执行证据</h2><button onClick={() => setEvidence(null)}>关闭</button></header>
          <p className="muted">证据读取时已核验 SHA-256；执行成功与判分通过分别记录，缺失值不补零。</p>
          {selected && (
            <dl className="detail-properties">
              <div><dt>数据集 / 样本</dt><dd>{selected.dataset_key} / {selected.sample_id}</dd></div>
              <div><dt>执行状态</dt><dd>{(STATUS[selected.state] ?? { label: "待确认" }).label}</dd></div>
              <div><dt>得分</dt><dd>{caseScore(meta)}</dd></div>
              <div><dt>错误类别</dt><dd>{selected.error_code || "无执行错误"}</dd></div>
              <div><dt>工具状态</dt><dd>{meta?.status || "—"}</dd></div>
              <div><dt>工具时间</dt><dd>{time(meta?.tool_started_at ?? null)} → {time(meta?.tool_finished_at ?? null)}</dd></div>
              <div><dt>工具耗时</dt><dd>{meta?.duration_sec != null ? `${meta.duration_sec} 秒` : "—"}</dd></div>
              <div><dt>Token</dt><dd>输入 {meta?.input_tokens ?? "—"} / 输出 {meta?.output_tokens ?? "—"}</dd></div>
              <div><dt>证据 SHA-256</dt><dd className="mono" title={selected.evidence_sha256 ?? ""}>{selected.evidence_sha256 ?? "—"}</dd></div>
            </dl>
          )}
          {selected?.error_code && <p className="notice">该次尝试存在执行异常。请检查服务连通性、资源环境及本次证据；结果未知时勿重复提交。</p>}
          {(meta?.diagnostics?.length ?? 0) > 0 && <p className="muted">诊断：{meta?.diagnostics.join("、")}</p>}
          {detail && !("message" in detail) && (
            <>
              <h3>模型输入</h3>
              <pre className="snapshot">{String(detail.prompt ?? "未提供")}</pre>
              <h3>模型响应</h3>
              <pre className="snapshot">{String(detail.response ?? "无响应")}</pre>
            </>
          )}
          <details>
            <summary>完整证据与标识</summary>
            <pre className="snapshot" data-testid="case-evidence">{JSON.stringify(evidence, null, 2)}</pre>
          </details>
        </dialog>, document.body)}
    </>
  );
  return initialRun ? (
    <section className="stack case-history">{body}</section>
  ) : (
    <details className="diagnostic-tasks case-history"><summary>Case 执行历史</summary>{body}</details>
  );
}
