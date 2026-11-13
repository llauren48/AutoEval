import { ListPagination } from "./ListPagination";
import { useEffect, useState } from "react";
import { ReportView, type Report } from "../app/ReportView";
import { apiGetPage } from "../services/api";

const validityLabels: Record<string, string> = {
  complete: "所选清单完整",
  partial: "部分有效",
  failed_summary: "执行失败摘要",
  invalid: "评分无效",
};

export function RealReports({ reportId, open }: { reportId?: string; open: (id: string) => void }) {
  const [query, setQuery] = useState("");
  const [model, setModel] = useState("");
  const [analysis, setAnalysis] = useState("");
  const [rows, setRows] = useState<Report[]>([]);
  const [offset, setOffset] = useState(0);
  const [pageSize,setPageSize] = useState(5);
  const [next, setNext] = useState<number | null>(null);
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    if (reportId) return;
    const controller = new AbortController();
    setRows([]);
    setNext(null);
    setMessage("");
    setLoading(true);
    apiGetPage<Report[]>(`/reports?limit=${pageSize}&offset=${offset}`, controller.signal)
      .then((page) => {
        setRows(page.data);
        setNext(page.next_offset);
      })
      .catch(() => {
        if (!controller.signal.aborted) setMessage("报告目录加载失败。");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [offset, refresh, reportId, pageSize]);

  if (reportId) {
    return (
      <ReportView
        key={reportId}
        id={reportId}
        go={(path) => {
          if (path === "reports") window.location.hash = "reports";
        }}
      />
    );
  }

  const analysisLabel = (value: string) => ({ completed: "分析成功", succeeded: "分析成功", failed: "分析失败", pending: "待分析", queued: "排队中", running: "分析中", not_enabled: "未启用", not_queued: "待排队", unresolved: "待查证", not_requested: "未分析" }[value] || "暂无分析");
  const filteredRows = rows.filter((row) => (!model || row.remote_model === model) && (!analysis || row.analysis_status === analysis) && `${row.id} ${row.run_id} ${row.remote_model}`.toLowerCase().includes(query.toLowerCase()));
  return (
    <div className="stack" data-testid="real-report-catalog">
      <section className="card report-filters filters" aria-label="当前页报告筛选">
        <label className="select-field"><span>模型</span><select value={model} onChange={(e) => setModel(e.target.value)}><option value="">全部模型</option>{[...new Set(rows.map((row) => row.remote_model))].map((value) => <option key={value}>{value}</option>)}</select></label>
        <label className="select-field"><span>分析状态</span><select value={analysis} onChange={(e) => setAnalysis(e.target.value)}><option value="">全部分析状态</option>{[...new Set(rows.map((row) => row.analysis_status))].map((value) => <option key={value} value={value}>{analysisLabel(value)}</option>)}</select></label>
        <input aria-label="搜索报告或任务" placeholder="搜索报告或任务" value={query} onChange={(e) => setQuery(e.target.value)} />
        <button onClick={() => setRefresh((value) => value + 1)}>刷新</button>
      </section>
      <section className="card table-card">
        <div className="report-list-heading"><h2>全部报告 <span className="badge">{rows.length}</span></h2><span className="muted">每次测评独立保留报告 · 当前页统计与筛选</span></div>
        {(message || loading) && <p role="status">{message || "报告加载中…"}</p>}
        <div className="table-wrap">
          <table className="reports-table">
            <thead>
              <tr>
                <th>报告 / 任务</th>
                <th>模型</th>
                <th>报告状态</th>
                <th>结果覆盖</th>
                <th>准确率</th>
                <th>生成时间</th><th>分析状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {filteredRows.map((row) => (
                <tr key={row.id}>
                  <td>
                    <b>{row.id.slice(0, 8)}</b>
                    <small title={row.run_id}>任务：{row.run_id.slice(0, 8)}</small>
                  </td>
                  <td>{row.remote_model}</td>
                  <td>
                    <span className={`coverage ${row.validity === "complete" ? "complete" : row.validity === "partial" ? "partial" : "error"}`}>
                      {validityLabels[row.validity] || "状态待确认"}
                    </span>
                    <small>探索测评 · 不适用发布门禁</small>
                    {row.evidence_status !== "verified" && <small>原始证据暂不可用，成绩不参与比较。</small>}
                  </td>
                  <td>
                    {row.summary.successful_samples}/{row.summary.expected_samples}
                    <small>
                      执行 {row.summary.executed_attempts} 次 · 失败 {row.summary.failed_samples} · 缺测 {row.summary.missing_samples}
                    </small>
                    <small>
                      GPQA · {row.summary.subset_scope === "lite" ? "Lite" : row.summary.subset_scope}
                    </small>
                  </td>
                  <td>
                    {row.evidence_status !== "verified" || row.summary.accuracy === null ? "—" : `${row.summary.accuracy}%`}
                    {row.validity === "partial" && <small>仅成功样本</small>}
                  </td>
                  <td>{new Date(row.created_at).toLocaleString("zh-CN")}</td><td><span className="badge">{analysisLabel(row.analysis_status)}</span></td>
                  <td>
                    <button className="link" onClick={() => open(row.id)}>
                      查看报告
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!loading && !filteredRows.length && !message && (
            <div className="empty">当前页没有符合条件的报告。</div>
          )}
        </div>
      </section>

      <ListPagination label="报告" offset={offset} nextOffset={next} pageSize={pageSize} onOffsetChange={setOffset} onPageSizeChange={setPageSize} disabled={loading} />
    </div>
  );
}
