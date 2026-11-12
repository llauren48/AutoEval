import { createPortal } from "react-dom";
import { useEffect, useRef, useState } from "react";
import { apiGet, apiGetPage } from "../services/api";
import { UnifiedOutputSummary } from "./UnifiedOutputSummary";

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
  tool_metadata?: { score?: number | null; tool_started_at: string | null; tool_finished_at: string | null; duration_sec: number | null; input_tokens: number | null; output_tokens: number | null; diagnostics: string[]; status: string } | null;
};

const caseScore = (metadata?: Attempt["tool_metadata"]) => typeof metadata?.score === "number" && Number.isFinite(metadata.score) ? String(metadata.score) : "未提供评分";
const time = (value: string | null) => value && !Number.isNaN(Date.parse(value)) ? new Date(value).toLocaleString("zh-CN") : "—";

export function CaseHistory({ initialRun = "" }: { initialRun?: string }) {
  const evidenceDialog = useRef<HTMLDialogElement>(null);
  const [run, setRun] = useState(initialRun);
  const [stateFilter, setStateFilter] = useState("");
  const [sample, setSample] = useState("");
  const [query, setQuery] = useState<{ run: string; sample: string; offset: number } | null>(null);
  const [rows, setRows] = useState<Attempt[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [selected, setSelected] = useState("");
  const [evidence, setEvidence] = useState<unknown>(null);
  useEffect(() => { if (evidence !== null) evidenceDialog.current?.showModal(); }, [evidence]);
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (initialRun) {
      setRun(initialRun);
      setQuery({ run: initialRun, sample: "", offset: 0 });
    }
  }, [initialRun]);
  useEffect(() => {
    if (!query) return;
    const controller = new AbortController();
    setRows([]); setNext(null); setSelected(""); setMessage("正在查询历史…");
    const suffix = `limit=20&offset=${query.offset}`;
    const path = `/runs/${encodeURIComponent(query.run)}/case-executions?${suffix}${query.sample ? `&sample_id=${encodeURIComponent(query.sample)}` : ""}`;
    apiGetPage<Attempt[]>(path, controller.signal)
      .then((page) => {
        setRows(page.data); setNext(page.next_offset);
        setMessage(page.data.length ? "历史已加载；评分结论请以报告为准。" : "该运行暂无匹配的 case 执行记录。");
      })
      .catch((error) => {
        if (!controller.signal.aborted) setMessage(error instanceof Error ? error.message : "历史查询失败");
      });
    return () => controller.abort();
  }, [query]);
  useEffect(() => {
    setEvidence(null);
    if (!selected) return;
    const controller = new AbortController();
    apiGet<unknown>(`/case-executions/${selected}/evidence`, controller.signal)
      .then(setEvidence)
      .catch((error) => {
        if (!controller.signal.aborted) setMessage(error instanceof Error ? error.message : "证据读取失败");
      });
    return () => controller.abort();
  }, [selected]);

  return (
    <details className="diagnostic-tasks case-history" open={Boolean(initialRun)}>
      <summary>Case 执行历史</summary>
      {initialRun && <UnifiedOutputSummary runId={initialRun} />}
      <p>查询每次执行尝试及已校验的原始证据。重试成功仍保留此前失败；执行状态与评分正确性分开。</p>
      <div className="filters">
        {!initialRun && <label>运行ID <input aria-label="回溯运行ID" value={run} onChange={(event) => setRun(event.target.value)} /></label>}
        <label>样本ID（可选） <input aria-label="回溯样本ID" value={sample} onChange={(event) => setSample(event.target.value)} /></label>
        <button type="button" disabled={!run.trim()} onClick={() => setQuery({ run: run.trim(), sample: sample.trim(), offset: 0 })}>查询case历史</button>
      </div>
      <label>当前页执行状态 <select aria-label="Case执行状态筛选" value={stateFilter} onChange={(e) => setStateFilter(e.target.value)}><option value="">全部尝试</option><option value="succeeded">执行成功</option><option value="failed">执行失败</option><option value="started">执行中 / 待查证</option></select></label>
      <p className="muted">模型答错属于评分结果；超时、断网等属于执行异常。此表保留每次尝试，评分结论以已校验证据为准。</p>
      <p role="status">{message}</p>
      {rows.length > 0 && (
        <div className="table-wrap" data-testid="case-attempts">
          <table>
            <thead><tr><th>数据集 / 样本</th><th>尝试</th><th>得分</th><th>执行状态</th><th>错误类别</th><th>创建时间</th><th>完成时间</th><th>证据</th></tr></thead>
            <tbody>{rows.filter((row) => !stateFilter || row.state === stateFilter).map((row) => (
              <tr key={row.id}>
                <td><b>{row.dataset_key}</b><small>{row.sample_id}</small></td>
                <td>第 {row.attempt_no} 次</td>
                <td>{caseScore(row.tool_metadata)}</td>
                <td><span className={`case-status ${row.state === "succeeded" ? "passed" : row.state === "failed" ? "error" : "running"}`}>{({ succeeded: "执行成功", failed: "执行失败", started: "执行中 / 待查证" } as Record<string,string>)[row.state] || "待确认"}</span></td>
                <td>{row.error_code || "无执行错误"}{row.error_code && <small>请检查服务连通性、资源环境及本次证据；结果未知时勿重复提交。</small>}</td>
                <td>{time(row.created_at)}</td>
                <td>{time(row.finished_at)}{row.tool_metadata && <details><summary>工具时间与 Token</summary><p>工具状态：{row.tool_metadata.status}</p><p>开始：{time(row.tool_metadata.tool_started_at)}<br />结束：{time(row.tool_metadata.tool_finished_at)}<br />耗时：{row.tool_metadata.duration_sec ?? "—"} 秒</p><p>输入：{row.tool_metadata.input_tokens ?? "—"} / 输出：{row.tool_metadata.output_tokens ?? "—"} Token</p>{row.tool_metadata.diagnostics.length > 0 && <p>诊断：{row.tool_metadata.diagnostics.join("、")}</p>}<small>工具报告值；与平台观测时间独立。缺失值未补零。</small></details>}</td>
                <td>
                  <button className="link" type="button" disabled={!row.evidence_available} onClick={() => { setSelected(row.id); setEvidence(null); }}>查看第{row.attempt_no}次证据</button>
                  <small>{row.evidence_sha256 ? "已归档，打开时核验完整性" : "尚未归档"}</small>
                </td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
      {rows.length > 0 && !rows.some((row) => !stateFilter || row.state === stateFilter) && <p className="empty">当前页没有符合条件的尝试</p>}
      {query && <div className="pagination"><button disabled={query.offset === 0} onClick={() => setQuery({ ...query, offset: Math.max(0, query.offset - 20) })}>上一页</button><button disabled={next === null} onClick={() => { if (next !== null) setQuery({ ...query, offset: next }); }}>下一页</button></div>}
      {evidence !== null && createPortal(
        <dialog ref={evidenceDialog} className="management-drawer" aria-label="Case 执行证据" onCancel={() => setEvidence(null)}>
          <header className="spread"><h2>Case 执行证据</h2><button onClick={() => setEvidence(null)}>关闭</button></header>
          <p>证据已通过完整性校验。执行成功与判分通过分别记录。</p>
          {typeof evidence === "object" && evidence !== null && <><h3>模型输入</h3><pre className="snapshot">{String((evidence as Record<string, unknown>).prompt ?? "未提供")}</pre><h3>模型响应</h3><pre className="snapshot">{String((evidence as Record<string, unknown>).response ?? "无响应")}</pre></>}
          <h3>完整证据与标识</h3><pre className="snapshot" data-testid="case-evidence">{JSON.stringify(evidence, null, 2)}</pre>
        </dialog>, document.body)}
    </details>
  );
}
