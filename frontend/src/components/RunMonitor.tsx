import { useEffect, useState } from "react";
import { apiGet } from "../services/api";
import { ManagementDrawer } from "./ManagementDrawer";

type Monitor = { collection_status: string; stale: boolean; source?: string; task_name?: string; process_id?: number; tool_status?: string; finish_count?: number | null; total_count?: number | null; observed_at?: string; snapshot_kind?: string; progress_description?: string; time_cost?: string; tool_start_time?: number };

export function RunMonitor({ runId, terminal }: { runId: string; terminal: boolean }) {
  const [evidence, setEvidence] = useState<unknown>(null);
  const [snapshots, setSnapshots] = useState<{ id: string; kind: string; created_at: string }[]>([]);
  const [value, setValue] = useState<Monitor | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    setValue(null); setEvidence(null); setSnapshots([]); setError(""); setLoading(true);
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

  const loadSnapshots = async () => {
    try {
      const response = await fetch(`/api/autoeval/v1/runs/${runId}/monitor/snapshots?limit=100`);
      if (!response.ok) throw new Error();
      setSnapshots((await response.json()).data);
    } catch { setError("监控归档目录暂不可用"); }
  };
  const loadEvidence = async (suffix: string) => {
    try { setEvidence(await apiGet(`/runs/${runId}/monitor/${suffix}`)); }
    catch { setEvidence({ message: "证据尚未归档或校验未通过" }); }
  };

  if (loading && !value) return <p className="muted" role="status">正在读取工具监控…</p>;
  if (!value) return <p className="muted" role="status">{error || "工具监控暂不可用。"}</p>;
  if (value.collection_status === "not_collected") return <p className="muted" role="status">{error || "本次运行未采集工具监控。"}</p>;
  const snapshotLabel = value.snapshot_kind === "final"
    ? "终态已归档"
    : value.snapshot_kind === "last_observed"
      ? "仅最后观测（非终态）"
      : "实时观测";
  return <section className="card stack"><h3>工具运行监控</h3><p>状态：{value.tool_status ?? "未知"} · 进度：{value.finish_count ?? "—"} / {value.total_count ?? "—"} · 进程：{value.process_id ?? "—"}</p><p>来源：{value.source} · 采集时间：{value.observed_at ? new Date(value.observed_at).toLocaleString() : "—"} · {snapshotLabel}{(value.stale || error) && " · 数据已过期 / 不可达"}</p>{error && <p role="status">{error}</p>}<p>工具开始时间：{value.tool_start_time ? new Date(value.tool_start_time * 1000).toLocaleString() : "—"} · 工具耗时：{value.time_cost ?? "—"} · 进度说明：{value.progress_description ?? "—"}</p><ManagementDrawer title="监控明细"><pre className="snapshot">{JSON.stringify(value, null, 2)}</pre><button type="button" onClick={() => void loadSnapshots()}>查看归档快照</button><button type="button" onClick={() => void loadEvidence("log")}>查看归档日志</button>{snapshots.map(row => <button key={row.id} type="button" className="link" onClick={() => void loadEvidence(`snapshots/${row.id}`)}>{row.kind} · {new Date(row.created_at).toLocaleString()}</button>)}{evidence !== null && <pre className="snapshot">{JSON.stringify(evidence, null, 2)}</pre>}</ManagementDrawer></section>;
}
