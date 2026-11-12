import officialDatasetMetadata from "../services/officialDatasetMetadata.json";
import { apiGet } from "../services/api";
import { aisbenchCatalog } from "../services/standardCatalog";
import { ManagementDrawer } from "./ManagementDrawer";
import { useEffect, useState } from "react";
import { datasetIdentity, datasetNote } from "../services/datasetIdentities";

type Row = { entry: { dataset: string; metric: string; unit: string; scope: string; source_name: string; source_version: string; conditions: Record<string, unknown> } };
export function WorkbenchDatasets({ revision }: { revision: number }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [message, setMessage] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {
    const controller = new AbortController(); setMessage("加载中…"); setRows([]);
    void (async () => {
      const result: Row[] = [];
      const standards = await aisbenchCatalog(controller.signal);
      for (const item of standards) {
        const raw = item.files["standard_config.json"]?.raw;
        result.push({ entry: { dataset: item.benchmark, metric: "未核验", unit: "—", scope: "标准约束（非成绩）", source_name: "标准仓输入", source_version: item.revision, conditions: { source_model: item.source_model, standard_config: raw ? JSON.parse(raw) : {} } } });
      }
      const registered = await apiGet<{ name: string; version: string; metadata: Record<string, unknown> }[]>("/standard-inputs/dataset-registrations", controller.signal);
      for (const item of registered) result.push({ entry: { dataset: item.name, metric: "待核验", unit: "—", scope: "已登记 · AISBench支持待核验 · 不可执行", source_name: "平台数据集登记", source_version: item.version, conditions: item.metadata } });
      if (!controller.signal.aborted) { setRows(result); setMessage(""); }
    })().catch(() => { if (!controller.signal.aborted) setMessage("数据集读取失败，请刷新重试。"); });
    return () => controller.abort();
  }, [revision]);
  const groups = new Map<string, Row[]>();
  for (const row of rows) {
    if (/fixture/i.test(row.entry.dataset)) continue;
    const key = datasetIdentity(row.entry.dataset);
    groups.set(key, [...(groups.get(key) || []), row]);
  }
  return <section className="card"><h2>数据集列表</h2><p className="muted">显示 AISBench 标准目录及人工登记的待核验项；测试夹具不在业务目录展示。待核验项独立标记，不进入可添加的 AISBench 目录，不代表已开放执行。</p><input aria-label="搜索数据集" placeholder="搜索数据集或原始名称" value={query} onChange={(e) => setQuery(e.target.value)} />{message && <p role="status">{message}</p>}<div className="table-wrap"><table><thead><tr><th>数据集</th><th>原始名称 / 核对说明</th><th>指标 / 单位</th><th>范围 / 版本</th><th>记录数</th><th>来源与条件</th></tr></thead><tbody>{[...groups].sort(([a], [b]) => a.localeCompare(b)).filter(([name, entries]) => [name, ...entries.map((r) => r.entry.dataset)].some((v) => v.toLowerCase().includes(query.toLowerCase()))).map(([name, entries]) => <tr key={name}><td><b>{name}</b>{name in officialDatasetMetadata && <small>官方资料已补充 · 执行适配另核验</small>}</td><td>{[...new Set(entries.map((r) => r.entry.dataset))].join("、")}<small>{datasetNote(name)}</small></td><td>{[...new Set(entries.map((r) => `${r.entry.metric} / ${r.entry.unit}`))].join("、")}</td><td>{[...new Set(entries.map((r) => `${r.entry.scope} / ${r.entry.conditions.dataset_version || "版本未披露"}`))].join("、")}</td><td>{entries.length}</td><td><ManagementDrawer title="查看"><h3>{name} · 来源与条件</h3>{name in officialDatasetMetadata && <section><h4>官方数据集资料</h4><pre className="snapshot">{JSON.stringify(officialDatasetMetadata[name as keyof typeof officialDatasetMetadata], null, 2)}</pre><a href={officialDatasetMetadata[name as keyof typeof officialDatasetMetadata].source} target="_blank" rel="noreferrer">查看官方来源</a></section>}{entries.map((r, i) => <div key={i}><p>{r.entry.dataset} · {r.entry.source_name} · {r.entry.source_version}</p><pre className="snapshot">{JSON.stringify(r.entry.conditions, null, 2)}</pre></div>)}</ManagementDrawer></td></tr>)}</tbody></table></div>{!message && !groups.size && <p className="empty">暂无数据集记录</p>}</section>;
}
