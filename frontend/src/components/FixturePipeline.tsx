import { useEffect, useRef, useState } from "react";
import { apiGetOptions, apiPost } from "../services/api";

export function FixturePipeline() {
  const [configs, setConfigs] = useState<{ id: string; display_name: string; version_label: string; kind: string }[]>([]);
  const [config, setConfig] = useState(""); const [run, setRun] = useState("");
  const [message, setMessage] = useState(""); const [busy, setBusy] = useState(false);
  const key = useRef(crypto.randomUUID());
  useEffect(() => { const c = new AbortController(); apiGetOptions<typeof configs>("/test-configs", c.signal).then((p) => setConfigs(p.data.filter((v) => v.kind === "standard"))).catch(() => setMessage("配置列表读取失败")); return () => c.abort(); }, []);
  return <section className="card stack"><h3>平台归档流程验证</h3><p>仅处理后台预配置的人工测试产物，不调用模型，不生成正式成绩，也不进入榜单。运行将经过 Outbox、Temporal、SSH/SFTP、对象存储与报告归档。</p><label>标准配置<select aria-label="流程验证标准配置" value={config} onChange={(e) => { setConfig(e.target.value); key.current = crypto.randomUUID(); setRun(""); }}><option value="">请选择标准配置</option>{configs.map((v) => <option key={v.id} value={v.id}>{v.display_name} / {v.version_label}</option>)}</select></label><button disabled={busy || !config || !!run} onClick={async () => { setBusy(true); setMessage(""); try { const r = await apiPost<{ run_id: string }>("/diagnostics/unified-fixture", { config_version_id: config }, key.current); setRun(r.run_id); setMessage("流程验证已提交，请查看任务；报告仅为测试来源。"); } catch { setMessage("提交未确认，请使用相同提交键重试查证；不会重复创建任务。"); } finally { setBusy(false); } }}>启动无模型流程验证</button>{message && <p role="status">{message}</p>}{run && <a className="link" href={`#run/${run}`}>查看流程验证任务</a>}</section>;
}
