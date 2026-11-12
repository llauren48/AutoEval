import { standardCatalog } from "../services/standardCatalog";
import { modelIdentities, isTestModel, isModelScopeId } from "../services/modelIdentities";
import { useEffect, useState } from "react";
import { apiGetPage, apiPost } from "../services/api";
import { ListPagination } from "./ListPagination";

type ModelRow = { key: string; names: Set<string>; sources: Set<string>; parameters: Set<string>; dates: Set<string>; datasets: Set<string>; mapped: boolean };
type Registered = { model_id: string; name: string };
type RecordRow = { state: string; entry: { model_name: string; dataset: string; source_type: string; conditions: Record<string, unknown> } };

export function WorkbenchModels({ revision }: { revision: number }) {
  const [localRevision, setLocalRevision] = useState(0);
  const [registering, setRegistering] = useState("");
  const register = async (catalogId: string) => {
    setRegistering(catalogId); setMessage("");
    try { await apiPost("/models/from-catalog", { catalog_id: catalogId }); setLocalRevision(v => v + 1); }
    catch (error) { setMessage(error instanceof Error ? error.message : "模型登记失败"); }
    finally { setRegistering(""); }
  };
  const [rows, setRows] = useState<ModelRow[]>([]);
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [pageSize, setPageSize] = useState(15);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setMessage(""); setRows([]);
    async function all<T>(path: string): Promise<T[]> {
      const result: T[] = [];
      let position: number | null = 0;
      while (position !== null) {
        const page: { data: T[]; next_offset: number | null } = await apiGetPage<T[]>(`${path}?offset=${position}&limit=100`, controller.signal);
        result.push(...page.data);
        if (page.next_offset !== null && page.next_offset <= position) throw Error("Invalid pagination");
        position = page.next_offset;
      }
      return result;
    }
    void Promise.all([all<Registered>("/models"), all<RecordRow>("/data-workbench/entries"), standardCatalog(controller.signal)]).then(([registered, records, standards]) => {
      if (controller.signal.aborted) return;
      const models = new Map<string, ModelRow>();
      const get = (key: string) => {
        let value = models.get(key);
        if (!value) { value = { key, names: new Set(), sources: new Set(), parameters: new Set(), dates: new Set(), datasets: new Set(), mapped: false }; models.set(key, value); }
        return value;
      };
      for (const model of registered) { if (isTestModel(model.model_id)) continue; const row = get(model.model_id); row.mapped ||= isModelScopeId(model.model_id); row.names.add(model.name); row.sources.add("平台登记"); }
      for (const record of records) {
        const e = record.entry;
        if (isTestModel(e.model_name)) continue;
        const id = e.conditions.modelscope_model_id;
        const validId = typeof id === "string" && isModelScopeId(id);
        const row = get(validId ? id : e.model_name);
        row.names.add(e.model_name); row.datasets.add(e.dataset);
        row.sources.add(({ official: "官方成绩", report: "平台报告", manual: "待确认来源" } as Record<string, string>)[e.source_type] || "其他来源");
        row.mapped ||= validId || Boolean(modelIdentities[e.model_name]);
        for (const [field, target] of [["parameter_count", row.parameters], ["model_release_date", row.dates]] as const) {
          const value = e.conditions[field];
          if (typeof value === "string" && !["unknown", "未披露", "未知"].includes(value)) target.add(value);
        }
      }
      for (const entry of standards) {
        const matches = [...models.values()].filter((row) => row.key === entry.source_model || row.names.has(entry.source_model));
        const row = matches.length === 1 ? matches[0] : get(entry.source_model);
        row.names.add(entry.source_model); row.sources.add("标准仓输入（未批准）"); row.datasets.add(entry.benchmark);
      }
      setRows([...models.values()].sort((a, b) => Number(Boolean(modelIdentities[b.key])) - Number(Boolean(modelIdentities[a.key])) || a.key.localeCompare(b.key))); setOffset(0);
    }).catch(() => { if (!controller.signal.aborted) setMessage("模型列表加载失败，请使用刷新数据重试；未展示不完整列表。"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [revision, localRevision]);
  const filtered = rows.filter((r) => `${r.key} ${[...r.names].join(" ")} ${modelIdentities[r.key]?.vendor || ""} ${r.key}`.toLowerCase().includes(query.toLowerCase()));
  return <section className="card stack">
    <div className="spread"><h2>模型列表 <span className="badge">{filtered.length}</span></h2><input aria-label="搜索工作台模型" placeholder="搜索模型名称或 ID" value={query} onChange={(e) => { setQuery(e.target.value); setOffset(0); }} /></div>
    <p className="muted">汇总平台登记、标准仓输入及榜单维护记录，按完整标识去重。官方名称未映射时独立保留；出现在此列表不代表可执行或已批准入榜。</p>
    {(loading || message) && <p role="status">{message || "正在加载模型…"}</p>}
    <div className="table-wrap"><table><thead><tr><th>模型</th><th>模型厂商</th><th>ModelScope 标识</th><th>登记状态</th><th>来源</th><th>发布时间</th><th>参数量</th><th>关联数据集</th></tr></thead><tbody>{filtered.slice(offset, offset + pageSize).map((row) => <tr key={row.key}><td><b>{[...row.names].find((name) => name !== row.key) || row.key.split("/").pop()}</b><small title={row.key}>{row.key}</small></td><td>{modelIdentities[row.key]?.vendor || "未确认"}</td><td>{row.mapped || modelIdentities[row.key] ? <a href={`https://modelscope.cn/models/${row.key}/summary`} target="_blank" rel="noreferrer">{row.key}</a> : "未映射"}</td><td>{row.sources.has("平台登记") ? "已登记（服务绑定另需版本）" : row.mapped || modelIdentities[row.key] ? "需先登记" : "需先确认 ModelScope 标识"}{!row.sources.has("平台登记") && row.sources.has("官方成绩") && isModelScopeId(row.key) && <button disabled={Boolean(registering)} onClick={() => void register(row.key)}>{registering === row.key ? "登记中…" : "登记模型身份"}</button>}</td><td>{[...row.sources].join("、")}</td><td>{[...row.dates].join("；") || "-"}</td><td>{[...row.parameters].join("；") || "-"}</td><td>{[...row.datasets].join("、") || "暂无成绩记录"}</td></tr>)}</tbody></table></div>
    {!loading && !message && !filtered.length && <p className="empty">暂无符合条件的模型</p>}
    <ListPagination label="模型" offset={offset} nextOffset={offset + pageSize < filtered.length ? offset + pageSize : null} pageSize={pageSize} onOffsetChange={setOffset} onPageSizeChange={setPageSize} disabled={loading} />
  </section>;
}
