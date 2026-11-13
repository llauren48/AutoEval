import { type DatasetChartResult, ReportCharts } from "../components/ReportCharts";
import { CopyIdentifier } from "../components/CopyIdentifier";
import { CaseHistory } from "../components/CaseHistory";
import { ManagementDrawer } from "../components/ManagementDrawer";
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { AnalysisPanel } from "../components/AnalysisPanel";
import { apiGet } from "../services/api";

type Summary = {
  expected_samples: number;
  executed_attempts: number;
  successful_samples: number;
  failed_samples: number;
  missing_samples: number;
  accuracy: number | null;
  subset_scope: string;
  dataset_samples: number;
  metric_scope: string;
  scoring_status?: string;
};

type BaselineComparison = {
  status: "matched" | "missing" | "incomparable";
  baseline_key: string | null;
  baseline_version: string | null;
  baseline_value: number | null;
  baseline_unit: string | null;
  baseline_direction: string | null;
  measured_value: number | null;
  difference: number | null;
};

export type Report = {
  dataset_results?: DatasetChartResult[];
  id: string;
  run_id: string;
  remote_model: string;
  validity: string;
  evidence_status: string;
  created_at: string;
  summary: Summary;
  sha256: string;
  snapshot_sha256: string;
  run_mode: string;
  execution_origin: string;
  gate_status: string;
  baseline_comparison: BaselineComparison;
  analysis_status: string;
  analysis_id: string | null;
};

const validityLabels: Record<string, string> = {
  complete: "所选清单完整",
  partial: "部分有效",
  failed_summary: "执行失败摘要",
  invalid: "评分无效",
};

const analysisLabels: Record<string, string> = {
  not_enabled: "未启用",
  not_queued: "待排队",
  queued: "排队中",
  running: "分析中",
  succeeded: "分析成功",
  failed: "分析失败",
  unresolved: "待查证",
};

function accuracyText(value: number | null) {
  return value === null ? "—" : `${value}%`;
}

function baselineValue(value: number | null, unit: string | null) {
  if (value === null) return "—";
  return `${value}${unit === "percent" ? "%" : unit ? ` ${unit}` : ""}`;
}

function evidenceText(value: unknown) {
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2) ?? "—";
}

export function ReportView({ id, go }: { id: string; go: (path: string) => void }) {
  const [reload, setReload] = useState(0);
  const [report, setReport] = useState<Report | null>(null);
  const [datasetOpen, setDatasetOpen] = useState(false);
  const [view, setView] = useState("总览");
  const [tab, setTab] = useState("结果分析");
  const [message, setMessage] = useState("");
  const [evidence, setEvidence] = useState<unknown>(null);
  const [evidenceRequested, setEvidenceRequested] = useState(false);
  const [evidenceOpen, setEvidenceOpen] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setDatasetOpen(false);
    setView("总览");
    setReport(null);
    setMessage("");
    setTab("结果分析");
    setEvidence(null);
    setEvidenceRequested(false);
    setEvidenceOpen(false);
    apiGet<Report>(`/reports/${id}`, controller.signal)
      .then(setReport)
      .catch(() => {
        if (!controller.signal.aborted) setMessage("报告不存在或无法读取。");
      });
    return () => controller.abort();
  }, [id, reload]);

  useEffect(() => {
    if (!evidenceRequested) return;
    const controller = new AbortController();
    setMessage("");
    apiGet(`/reports/${id}/evidence`, controller.signal)
      .then(setEvidence)
      .catch(() => {
        if (!controller.signal.aborted) {
          setMessage("报告证据不可用或完整性校验失败；不能据此确认报告有效。");
        }
      });
    return () => controller.abort();
  }, [id, evidenceRequested]);

  useEffect(() => {
    if (!evidenceOpen) return;
    const prior = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setEvidenceOpen(false);
    };
    document.addEventListener("keydown", close);
    return () => {
      document.body.style.overflow = prior;
      document.removeEventListener("keydown", close);
    };
  }, [evidenceOpen]);

  if (!report) {
    return (
      <div className="stack report-workspace">
        <div className="report-navigation-bar">
          <button className="link back" onClick={() => go("reports")}>
            返回报告列表
          </button>
        </div>
        <section className="card">
          <p role="status">{message || "报告加载中…"}</p>{message && <button onClick={() => setReload((value) => value + 1)}>重新加载报告</button>}
        </section>
      </div>
    );
  }

  const summary = { ...report.summary, accuracy: report.evidence_status === "verified" ? report.summary.accuracy : null };
  const baseline = report.baseline_comparison;
  const comparability = report.evidence_status !== "verified" ? "原始证据不可用，无法确认可比性" : baseline.status === "matched" ? "比较条件一致" : baseline.status === "missing" ? "未配置匹配基准" : "清单、工具版本或执行参数不一致";
  const coverage =
    report.validity === "complete"
      ? "complete"
      : report.validity === "partial"
        ? "partial"
        : "error";
  const stats: Array<[string, number | string]> = [
    ["数据集", 1],
    ["完整完成（历史）", report.validity === "complete" ? 1 : 0],
    ["部分完成（历史）", report.validity === "partial" ? 1 : 0],
    ["执行失败（历史）", report.validity === "failed_summary" ? 1 : 0],
    ["总耗时", "暂无记录"],
    ["证据状态", report.evidence_status === "verified" ? "已校验" : "不可用"],
  ];
  const difference =
    baseline.status === "matched" && baseline.difference !== null
      ? `${baseline.difference > 0 ? "+" : ""}${baseline.difference}${baseline.baseline_unit === "percent" ? "pp" : baseline.baseline_unit ? ` ${baseline.baseline_unit}` : ""}`
      : "—";

  return (
    <div className="stack report-workspace" data-testid="real-report-detail">
      {report.evidence_status !== "verified" && <p role="alert" className="notice" data-testid="report-evidence-health">原始报告证据缺失、不可用或未通过校验，当前仅保留历史索引；不展示有效成绩、不参与榜单。历史归档状态不代表当前证据完整。</p>}
      <div className="report-navigation-bar">
        <nav aria-label="报告返回导航" className="report-return-links">
          <button className="link" onClick={() => go("reports")}>
            返回报告列表
          </button>
          {datasetOpen && <button className="link" onClick={() => setDatasetOpen(false)}>返回报告总览</button>}
        </nav>
        <a className="primary button" href={`#run/${report.run_id}`}>查看任务</a>
      </div>

      {!datasetOpen && <section className="card restored-detail">
        <div className="spread">
          <h2>
            报告总览 <span className="badge" title={report.id}>{report.id.slice(0, 8)}</span>
          </h2>
          <span className={`coverage ${coverage}`}>
            {validityLabels[report.validity] || "待确认"}
          </span>
        </div>
        <div className="tabs report-view-tabs" role="tablist" aria-label="报告视图">
          {["总览", "智能分析"].map((name) => (
            <button
              key={name}
              role="tab"
              aria-selected={view === name}
              className={view === name ? "active" : ""}
              onClick={() => setView(name)}
            >
              {name}
              {name === "智能分析" && (
                <span className={`badge ${report.analysis_status === "succeeded" ? "green" : ["failed", "unresolved"].includes(report.analysis_status) ? "orange" : "neutral"}`}>
                  {analysisLabels[report.analysis_status] || "待确认"}
                </span>
              )}
            </button>
          ))}
        </div>
        {view === "总览" && <>
        <div className="execution-meta">
          <span>模型 <b>{report.remote_model}</b></span>
          <span>生成时间 <b>{new Date(report.created_at).toLocaleString("zh-CN")}</b></span>
          <span>运行方式 <b>串行 · 探索</b></span>
          <span>范围 <b>Lite</b></span>
          <span>发布门禁 <b>不适用</b></span><span>分析状态 <b>{analysisLabels[report.analysis_status] || "待确认"}</b></span>
        </div>
        <div className="report-summary-stats table-wrap">
          {stats.map(([label, value]) => (
            <div key={label}>
              <span>{label}</span>
              <strong>{value}</strong>
            </div>
          ))}
        </div>
        {report.validity === "partial" && (
          <div className="notice">
            准确率仅覆盖成功样本；缺测与失败样本不按零分补入，不能替代完整清单成绩。
          </div>
        )}
        <div className="notice">
          当前清单 {summary.expected_samples} 个样本 / 数据集 {summary.dataset_samples} 个样本；本报告不代表全量成绩。执行完成不代表报告有效，成绩须结合证据完整性判断。
        </div>
        <div className="report-quick-links">
          <a className="report-quick-link" href={`#run/${report.run_id}`}>
            查看来源任务与 Case 历史
          </a>
          <ManagementDrawer title="报告标识与校验值">
            <div className="table-wrap">
              <table>
                <tbody>
                  <tr><th>报告 ID</th><td>{report.id} <CopyIdentifier value={report.id} /></td></tr>
                  <tr><th>报告 sha256</th><td>{report.sha256}</td></tr>
                  <tr><th>快照 sha256</th><td>{report.snapshot_sha256}</td></tr>
                </tbody>
              </table>
            </div>
          </ManagementDrawer>
        </div>
        <ReportCharts report={report} />
        {report.execution_origin === "simulation" && <p className="notice">人工测试产物流程验证报告；不代表真实模型成绩，不参与榜单或正式发布门禁。</p>}
        <h3>数据集结果</h3>
        <div className="table-wrap"><table><thead><tr><th>数据集</th><th>类型</th><th>覆盖</th><th>核心指标</th><th>指标值</th><th>基准差异</th><th>操作</th></tr></thead><tbody><tr><td>GPQA</td><td>科学问答</td><td>{summary.successful_samples}/{summary.expected_samples} · {validityLabels[report.validity]}</td><td>准确率</td><td>{accuracyText(summary.accuracy)}</td><td>{report.evidence_status !== "verified" ? "证据不可用" : baseline.status === "missing" ? "暂无基准" : baseline.status === "incomparable" ? "不可直接比较" : difference}</td><td><button className="link" onClick={() => setDatasetOpen(true)}>查看结果</button></td></tr></tbody></table></div>
        </>}
        {view === "智能分析" && <section className="report-analysis-view">
          <p className="muted">{report.evidence_status !== "verified" ? "原始证据不可用，当前无法确认有效成绩或生成可信比较结论。" : "本次为所选 Lite 清单的探索结果，不代表全量成绩，不适用正式发布门禁。"}</p>
          <AnalysisPanel reportId={report.id} status={report.analysis_status} executionOrigin={report.execution_origin} onStatus={(state) => setReport((current) => current ? { ...current, analysis_status: state } : current)} />
        </section>}
      </section>}

      {datasetOpen && <section className="card stack report-dataset-result restored-detail">
        <div className="spread">
          <div>
            <h2>GPQA</h2>
            <p className="muted">
              科学问答 · Lite · 有效样本 {summary.successful_samples} / {summary.expected_samples}
            </p>
          </div>
          <span className={`coverage ${coverage}`}>{validityLabels[report.validity]}</span>
        </div>
        <div className="tabs">
          {["结果分析", "样本明细", "报告证据"].map((name) => (
            <button
              className={tab === name ? "active" : ""}
              key={name}
              onClick={() => setTab(name)}
            >
              {name}
            </button>
          ))}
        </div>

        {tab === "结果分析" ? (
          <>
            <div className="report-quality-metrics">
              <div>
                <span>准确率{report.validity === "partial" ? "（仅成功样本）" : ""}</span>
                <strong>{accuracyText(summary.accuracy)}</strong>
              </div>
              <div>
                <span>成功 / 预期</span>
                <strong>
                  {summary.successful_samples}<small> / {summary.expected_samples}</small>
                </strong>
              </div>
              <div>
                <span>评分状态</span>
                <strong>{report.evidence_status !== "verified" ? "待核验" : summary.scoring_status === "succeeded" ? "评分完成" : summary.scoring_status === "failed" ? "评分失败" : "暂无评分"}</strong>
              </div>
            </div>

            <div data-testid="baseline-comparison">
              <h3>基准对比</h3><p className="muted">可比性：{comparability}。基准版本：{baseline.baseline_key ? `${baseline.baseline_key} / ${baseline.baseline_version || "未指定版本"}` : "暂无匹配基准"}。</p>
              {baseline.status === "missing" && (
                <p>基准对比：暂无基准；不以零值替代，不计算差值。</p>
              )}
              {baseline.status === "incomparable" && (
                <p>基准对比：测试条件不一致，不可直接比较；不计算差值。</p>
              )}
              {baseline.status === "matched" && (
                <p>
                  基准对比：{baseline.baseline_key} {baseline.baseline_version}；基准 {baselineValue(baseline.baseline_value, baseline.baseline_unit)}，实测 {baselineValue(baseline.measured_value, baseline.baseline_unit)}，差值 {difference}。
                </p>
              )}
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr><th>指标</th><th>实测值</th><th>基准值</th><th>差异</th><th>比较条件</th></tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td>准确率<small>所选样本清单</small></td>
                      <td>{accuracyText(summary.accuracy)}</td>
                      <td>{report.evidence_status === "verified" ? baselineValue(baseline.baseline_value, baseline.baseline_unit) : "—"}</td>
                      <td>{report.evidence_status === "verified" ? difference : "—"}</td>
                      <td>
                        {baseline.status === "matched"
                          ? `${baseline.baseline_key} ${baseline.baseline_version} · ${baseline.baseline_direction}`
                          : baseline.status === "missing"
                            ? "无匹配基准"
                            : "测试条件不一致，不可直接比较"}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

            <p className="muted report-local-analysis">
              智能分析已独立为报告级页签。
              <button className="link" onClick={() => { setDatasetOpen(false); setView("智能分析"); }}>前往智能分析</button>
            </p>
          </>
        ) : tab === "样本明细" ? <CaseHistory initialRun={report.run_id} /> : (
          <section>
            <div className="notice">
              证据仅在点击后读取，并由后端校验完整性；以下按原始文本或 JSON 展示。执行 succeeded 不代表逐 case 判分通过。
            </div>
            <p>
              <button
                onClick={() => {
                  setEvidenceOpen(true);
                  if (!evidenceRequested) setEvidenceRequested(true);
                }}
              >
                读取并校验报告证据
              </button>
            </p>
            <p role="status">{message}</p>
          </section>
        )}
      </section>}

      {evidenceOpen &&
        createPortal(
          <div className="case-drawer-backdrop" onClick={() => setEvidenceOpen(false)}>
            <section
              className="case-drawer"
              role="dialog"
              aria-modal="true"
              aria-label="报告证据详情"
              onClick={(event) => event.stopPropagation()}
            >
              <div className="spread">
                <h2>报告证据 · {report.id.slice(0, 8)}</h2>
                <button autoFocus onClick={() => setEvidenceOpen(false)}>关闭</button>
              </div>
              <p>报告证据已通过后端校验后原样返回，不生成额外判分或分析结论。</p>
              {evidence !== null ? (
                <pre className="snapshot" data-testid="real-report-evidence">
                  {evidenceText(evidence)}
                </pre>
              ) : (
                <p role="status">{message || "正在读取并校验报告证据…"}</p>
              )}
            </section>
          </div>,
          document.body,
        )}
    </div>
  );
}
