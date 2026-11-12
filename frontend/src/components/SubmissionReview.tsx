import { useEffect, useRef, useState } from "react";
import { apiGet, apiPost } from "../services/api";
import type { Config, Resource, Target } from "../services/evaluation";

type Spec = { bindings_complete: boolean; spec: { items: { sample_count: number; manifest_id: string; tool: unknown }[] } };
export function SubmissionReview({ config, target, resource, resourceKind, busy, submit }: {
  config: Config | null; target: Target | null; resource: Resource | null; resourceKind: string;
  busy: boolean; submit: () => void;
}) {
  const [checks, setChecks] = useState<{ label: string; ok: boolean; message: string }[]>([]);
  const [spec, setSpec] = useState<Spec | null>(null);
  const [checking, setChecking] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [checkedAt, setCheckedAt] = useState(0);
  const generation = useRef(0);
  useEffect(() => {
    generation.current++; setChecks([]); setSpec(null); setConfirmed(false); setCheckedAt(0); setChecking(false);
    return () => { generation.current++; };
  }, [config, target, resource, resourceKind]);
  const precheck = async () => {
    if (!config || !target || !resource || checking) return;
    const revision = ++generation.current;
    setChecking(true); setChecks([]); setConfirmed(false); setCheckedAt(0);
    const results = await Promise.allSettled([
      apiPost<{ outcome: string }>(`/targets/${target.id}/connection-tests`, {}),
      apiPost<{ environment_ready: boolean; evaluation_ready: boolean }>(`${resourceKind === "aisbench_monitor" ? "/aisbench-monitored/resources" : resourceKind === "customer_ssh" ? "/ssh-resources" : "/resources"}/${resource.id}/validate`, {}),
      apiGet<Spec>(`/test-configs/${config.id}/execution-spec`),
    ]);
    if (generation.current !== revision) return;
    const [model, host, configuration] = results;
    const modelOK = model.status === "fulfilled" && model.value.outcome === "matched";
    const hostOK = host.status === "fulfilled" && host.value.environment_ready && (resourceKind !== "customer_ssh" || host.value.evaluation_ready);
    const value = configuration.status === "fulfilled" ? configuration.value : null;
    const params = config.items.GPQA;
    const configOK = !!value?.bindings_complete && value.spec.items.length === 1 && value.spec.items[0].sample_count > 0 && value.spec.items[0].sample_count <= 5 && params?.scope === "Lite" && params.output > 0 && params.output <= (resourceKind === "aisbench_monitor" ? 2048 : 64) && params.concurrency === 1 && params.temperature === 0 && params.top_p === 1;
    setSpec(value);
    setChecks([
      { label: "模型服务", ok: modelOK, message: modelOK ? "模型列表可达，目标模型存在；未发送生成请求" : "连接未通过，请在管理模型服务中检查地址、凭据和模型名" },
      { label: "执行资源", ok: hostOK, message: hostOK ? resourceKind === "customer_ssh" ? "SSH、主机指纹、SFTP及工具预检通过" : "容器就绪；原生工具由执行端再次校验" : "资源未就绪，请在管理执行资源中检查主机指纹、目录和工具版本" },
      { label: "配置与清单", ok: configOK, message: configOK ? "绑定完整，样本量及参数符合探索限制；提交时复核原始证据和工具" : "绑定、样本量或参数不符合限制，请维护配置及执行规则" },
    ]);
    setCheckedAt(Date.now()); setChecking(false);
  };
  const ready = checks.length === 3 && checks.every((row) => row.ok);
  return <section className="submission-review stack" aria-label="提交前检查">
    <button disabled={checking || busy || !config || !target || !resource} onClick={() => void precheck()}>{checking ? "检查中…" : "检查服务、资源与配置"}</button>
    <p className="muted">预检不调用模型生成；检查结果仅代表检查时状态，提交与执行仍会复核。</p>
    {checks.map((row) => <p key={row.label} role={row.ok ? "status" : "alert"}><b>{row.label} · {row.ok ? "通过" : "未通过"}</b><small>{row.message}</small></p>)}
    {checkedAt > 0 && <small>检查时间：{new Date(checkedAt).toLocaleTimeString("zh-CN")}</small>}
    {spec && <details><summary>本次执行范围</summary><dl><dt>模型 / 配置版本</dt><dd>{target?.remote_model} / {config?.version_label}</dd><dt>资源</dt><dd>{resource?.binding_key}</dd><dt>GPQA Lite 样本数</dt><dd>{spec.spec.items[0]?.sample_count ?? "未配置"}</dd><dt>请求上限 / 回答长度上限</dt><dd>5 次 / {config?.items.GPQA?.output} Token</dd><dt>样本清单</dt><dd>{spec.spec.items[0]?.manifest_id || "未绑定"}</dd></dl></details>}
    <label className="inline"><input type="checkbox" checked={confirmed} disabled={!ready} onChange={(e) => setConfirmed(e.target.checked)} />确认本次范围及资源，开始探索测评</label>
    <button className="primary" disabled={!ready || !confirmed || checking || busy} onClick={() => {
      if (Date.now() - checkedAt > 300000) { setConfirmed(false); setChecks([]); setCheckedAt(0); return; }
      submit();
    }}>发起测评</button>
    <small>检查超过 5 分钟需重新执行。探索结果不用于正式发布门禁。</small>
  </section>;
}
