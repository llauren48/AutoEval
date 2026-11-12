import { useEffect, useState } from "react";
import { apiGet } from "../services/api";

type Summary = { validity: string; observed_cases: number; expected_cases: number; missing_cases: number; token_coverage: { known_attempts: number; total_attempts: number; input_tokens_known_sum: number | null; output_tokens_known_sum: number | null } };
export function UnifiedOutputSummary({ runId, compact = false }: { runId: string; compact?: boolean }) {
  const [result, setResult] = useState<Summary | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    setResult(null); setError(""); const controller = new AbortController();
    apiGet<Summary | null>(`/runs/${runId}/unified-output`, controller.signal).then((value) => { if (value && !value.token_coverage) throw Error("统一输出结构不匹配"); if (!controller.signal.aborted) setResult(value); }).catch(() => { if (!controller.signal.aborted) setError("统一输出证据暂不可用"); });
    return () => controller.abort();
  }, [runId]);
  if (error) return compact ? null : <p className="notice" role="status">{error}</p>;
  if (!result) return null;
  const tokens = result.token_coverage;
  if (compact) return <p className="muted unified-summary" title="包含已归档失败尝试；缺失值未补零。此处不代表推理性能、正式成绩或发布门禁结论。">统一输出：覆盖 {result.observed_cases} / {result.expected_cases} 个 case · 缺测 {result.missing_cases} · Token 覆盖 {tokens.known_attempts} / {tokens.total_attempts} 次（输入 {tokens.input_tokens_known_sum ?? "—"} / 输出 {tokens.output_tokens_known_sum ?? "—"}）</p>;
  return <section className="card stack"><h3>统一输出覆盖情况</h3><p>最近一次收集：{result.observed_cases} / {result.expected_cases} 个 case；缺测 {result.missing_cases} 个。</p><p>Token 记录覆盖 {tokens.known_attempts} / {tokens.total_attempts} 次尝试。输入已知合计：{tokens.input_tokens_known_sum ?? "—"}；输出已知合计：{tokens.output_tokens_known_sum ?? "—"}。</p><small>包含已归档失败尝试；缺失值未补零。此处不代表推理性能、正式成绩或发布门禁结论。</small></section>;
}
