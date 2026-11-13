import { useEffect, useState } from "react";
import { apiGet, apiPost } from "../services/api";

type Statement = { statement: string; evidence_ids: string[] };
type Analysis = {
  id: string;
  state: string;
  retry_count: number;
  error_code: string | null;
  started_at: string | null;
  finished_at: string | null;
  result: null | {
    state: string;
    error_code: string | null;
    analysis: null | { summary: string; evidence_ids: string[]; facts: Statement[]; inferences: Statement[]; recommended_actions: string[] };
  };
};

export function AnalysisPanel({ reportId, status, executionOrigin, onStatus }: { reportId: string; status: string; executionOrigin: string; onStatus: (state: string) => void }) {
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [revision, setRevision] = useState(0);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setAnalysis(null); setMessage("");
  }, [reportId]);
  useEffect(() => {
    if (executionOrigin !== "real" || ["not_enabled", "not_queued"].includes(status)) return;
    const controller = new AbortController();
    apiGet<Analysis>(`/reports/${reportId}/analysis`, controller.signal).then((row) => {
      setAnalysis(row); onStatus(row.state);
    }).catch(() => { if (!controller.signal.aborted) setMessage("分析结果或校验值读取失败；原始报告不受影响。"); });
    return () => controller.abort();
  }, [reportId, status, executionOrigin, revision]);
  useEffect(() => {
    if (!analysis || !["queued", "running"].includes(analysis.state)) return;
    const timer = setTimeout(() => setRevision((value) => value + 1), 2000);
    return () => clearTimeout(timer);
  }, [analysis]);
  const queue = async (action: "start" | "retry") => {
    if (action === "retry" && !window.confirm("重新分析将发起新的模型调用，可能产生费用。上次待查证的调用可能已计费，是否继续？")) return;
    setBusy(true); setMessage("");
    try {
      const row = await apiPost<Analysis>(`/reports/${reportId}/analysis:${action}`);
      setAnalysis(row); onStatus(row.state); setRevision((value) => value + 1);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "分析未排队；请先查证当前状态。");
    } finally { setBusy(false); }
  };
  if (executionOrigin !== "real") {
    return <section className="analysis-panel" data-testid="analysis-status"><p>非真实执行来源，不启用智能分析；原始报告不受影响。</p></section>;
  }
  const statements = (title: string, rows: Statement[], tone: string) => <section className={`analysis-section ${tone}`}><header><h4>{title}</h4><span className="badge neutral">{rows.length} 条</span></header>{rows.length ? <ol className="analysis-statements">{rows.map((row, index) => <li key={index}><p>{row.statement}</p><details className="analysis-citations"><summary>证据引用 · {row.evidence_ids.length}</summary><div>{row.evidence_ids.length ? row.evidence_ids.map((id, i) => <code key={i}>{id}</code>) : "证据不足"}</div></details></li>)}</ol> : <p className="muted">暂无内容</p>}</section>;

  return <section className="analysis-panel" data-testid="analysis-status">
    <p className="muted">分析独立于测评执行。重试只重新分析已有证据，不重测、不修改原始报告；模型分析可能产生调用费用。</p>
    {status === "not_enabled" && <p>未配置独立分析目标与资源；原始报告不受影响。</p>}
    {status === "not_queued" && <><p>尚未启动智能分析，请点击按钮手动发起。</p><button disabled={busy} onClick={() => void queue("start")}>开始智能分析</button></>}
    {analysis && <div className="analysis-content">
      <p className="analysis-state">状态：{({ queued: "排队中", running: "分析中", succeeded: "分析成功", completed: "分析成功", failed: "分析失败", unresolved: "结果待查证" } as Record<string, string>)[analysis.state] || "待确认"}；重试次数：{analysis.retry_count}</p>
      {analysis.started_at && <p>开始：{new Date(analysis.started_at).toLocaleString("zh-CN")}；完成：{analysis.finished_at ? new Date(analysis.finished_at).toLocaleString("zh-CN") : "尚未完成"}</p>}
      {analysis.error_code && <p className="analysis-error" role="alert">错误类别：{analysis.error_code}</p>}
      {analysis.state === "unresolved" && <p className="analysis-unresolved" role="alert">上次调用结果待查证，可手动重新分析；原调用可能已产生费用。</p>}
      {["failed", "unresolved"].includes(analysis.state) && <button disabled={busy} onClick={() => void queue("retry")}>重新分析</button>}
      {analysis.result?.analysis && <div data-testid="analysis-result">
        <section className="analysis-summary"><h4>分析摘要</h4><p>{analysis.result.analysis.summary}</p></section><div className="analysis-result-grid">
        {statements("报告事实", analysis.result.analysis.facts, "facts")}
        {statements("Agent 推断（非原始成绩）", analysis.result.analysis.inferences, "inferences")}
        </div><section className="analysis-section analysis-recommendations"><header><h4>建议</h4><span className="badge neutral">{analysis.result.analysis.recommended_actions.length} 条</span></header><ol>{analysis.result.analysis.recommended_actions.map((action, index) => <li key={index}>{action}</li>)}</ol></section>
        <details className="analysis-raw"><summary>分析证据引用与原始响应</summary><p>{analysis.result.analysis.evidence_ids.join("、")}</p><pre className="snapshot">{JSON.stringify(analysis.result, null, 2)}</pre></details>
      </div>}
    </div>}
    {message && <p role="alert">{message} <button onClick={() => setRevision((value) => value + 1)}>重新查询分析状态</button></p>}
  </section>;
}
