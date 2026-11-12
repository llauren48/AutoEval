import { aisbenchCatalog } from "../services/standardCatalog";
import { AdapterFields } from "../components/AdapterFields";
import { CustomAdapterEditor } from "../components/CustomAdapterEditor";
import { StandardInputs } from "../components/StandardInputs";
import { SearchableSelect } from "../components/SearchableSelect";
import { SubmissionReview } from "../components/SubmissionReview";
import { ManagementDrawer } from "../components/ManagementDrawer";
import { PageNumbers } from "../components/PageNumbers";
import { useEffect, useState } from "react";
import { ModelRegistry } from "../components/ModelRegistry";
import { ServiceBindings } from "../components/ServiceBindings";
import { SSHResources } from "../components/SSHResources";
import {
  type Config, type Resource, type Target, eligibleConfig, eligibleTarget,
  useDefaultConfig, useExplorationSubmission, useOptions, withSelected,
} from "../services/evaluation";

type Param = {
  adapter_input?: Record<string, unknown>;
  output: number;
  concurrency: number;
  temperature: number;
  top_p: number;
  scope: string;
};
type Profile = {
  provenance?: Record<string, { url?: string | null; revision?: string | null; sha256?: string | null }>;
  persistedVersion?: number;
  id: string;
  name: string;
  source: string;
  model?: string;
  modelId?: string;
  official: boolean;
  versions: string[];
  items: string[];
  data?: Record<string, Record<string, Param>>;
};
const names = ["GPQA", "MMMU-Pro", "AutomationBench"];
const datasetTypes = [
  { label: "科学问答", items: ["GPQA", "GPQA_Diamond"] },
  { label: "多模态", items: ["MMMU-Pro"] },
  { label: "Agent 工作流", items: ["DeepSWE", "AutomationBench"] },
];

function DatasetTree({
  items,
  selected,
  disabled = [],
  active,
  onSelect,
  onActive,
  scope,
}: {
  items: string[];
  selected?: string[];
  disabled?: string[];
  active?: string;
  onSelect?: (items: string[]) => void;
  onActive?: (name: string) => void;
  scope?: string;
}) {
  return (
    <div className="dataset-tree" aria-label="按类型分组的数据集">
      {datasetTypes.map((group) => {
        const children = group.items.filter((d) => items.includes(d));
        if (!children.length) return null;
        return (
          <details open key={group.label} className="dataset-tree-group">
            <summary>{group.label}<span>{children.length}</span></summary>
            <div className="dataset-tree-children">
              {children.map((d) => (
                <div key={d} className={`dataset-tree-row ${active === d ? "is-active" : ""}`}>
                  {onSelect && (
                    <input
                      aria-label={`选择 ${d}`}
                      type="checkbox"
                      disabled={disabled.includes(d)}
                      checked={selected?.includes(d) || disabled.includes(d)}
                      onChange={(e) => onSelect(
                        e.target.checked
                          ? [...(selected || []).filter((x) => x !== d), d]
                          : (selected || []).filter((x) => x !== d),
                      )}
                    />
                  )}
                  {onActive ? (
                    <button className="dataset-tree-name" title={d} onClick={() => onActive(d)}>{d}</button>
                  ) : (
                    <label
                      className="dataset-tree-name"
                      onClick={() => {
                        if (onSelect && !disabled.includes(d)) onSelect(
                          selected?.includes(d) ? selected.filter((x) => x !== d) : [...(selected || []), d],
                        );
                      }}
                    >{d}</label>
                  )}
                  {scope && <span className="dataset-tree-meta">{scope}</span>}
                  {disabled.includes(d) && <span className="dataset-tree-meta">已添加</span>}
                </div>
              ))}
            </div>
          </details>
        );
      })}
      {!items.length && <div className="empty">未找到匹配的数据集</div>}
    </div>
  );
}

const base = (i = 0): Param => ({
  output: 2048,
  concurrency: 60 - i * 20,
  temperature: 0.6,
  top_p: 0.95,
  scope: "全量",
});

export function useProfiles() {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/autoeval/v1/test-configs?limit=200", { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("配置服务不可用");
        return response.json();
      })
      .then((body) => {
        const grouped = new Map<string, Profile>();
        for (const item of body.data as Config[]) {
          const official = item.kind === "standard";
          const existing = grouped.get(item.config_key);
          if (existing) {
            existing.versions.push(item.version_label);
            existing.data![item.version_label] = item.items;
            existing.provenance![item.version_label] = { url: item.source_url, revision: item.source_revision, sha256: item.source_sha256 };
          } else grouped.set(item.config_key, {
            id: item.config_key,
            name: item.display_name,
            source: official ? "标准仓库" : "后端持久化",
            official,
            persistedVersion: item.version,
            versions: [item.version_label],
            items: Object.keys(item.items),
            data: { [item.version_label]: item.items },
            provenance: { [item.version_label]: { url: item.source_url, revision: item.source_revision, sha256: item.source_sha256 } },
          });
        }
        setProfiles([...grouped.values()]);
      }).catch(() => { if (!controller.signal.aborted) setProfiles([]); });
    return () => controller.abort();
  }, []);
  return [profiles, setProfiles] as const;
}

function Note({ children }: { children: React.ReactNode }) {
  return <div className="notice">{children}</div>;
}

function ParameterCards({
  active,
  items,
  params,
  onChange,
  readonly = false,
}: {
  items: string[];
  params: Record<string, Param>;
  onChange: (p: Record<string, Param>) => void;
  readonly?: boolean;
  active: string;
}) {
  return (
    <div className="stack">
      {items.filter((d) => d === active).map((d) => {
        const value = params[d] || base();
        return (
          <section className="card" key={d}>
            <h3>{d}{readonly ? " · 只读" : ""}</h3>
            <AdapterFields value={value} readonly={readonly} onChange={(next) => onChange({ ...params, [d]: next as Param })} />
          </section>
        );
      })}
      {!items.length && <div className="empty">请从左侧选择数据集</div>}
    </div>
  );
}

function ConfigurationDrawer({ children }: { children: React.ReactNode }) {
  return (
    <div className="workflow-backdrop">
      <section className="workflow-drawer" role="dialog" aria-modal="true" aria-label="配置编辑">
        {children}
      </section>
    </div>
  );
}

export function Configurations({ profiles, setProfiles, go, notify }: {
  profiles: Profile[];
  setProfiles: (p: Profile[]) => void;
  go: (p: string) => void;
  notify: (s: string) => void;
}) {
  const [catalogNames, setCatalogNames] = useState<string[]>([]);
  const [catalogError, setCatalogError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    aisbenchCatalog(controller.signal).then((rows) => setCatalogNames([...new Set(rows.map((row) => row.benchmark))])).catch(() => { if (!controller.signal.aborted) setCatalogError("AISBench 数据集目录加载失败，请重新打开页面。"); });
    return () => controller.abort();
  }, []);
  const [kind, setKind] = useState("标准配置");
  const [officialVersion, setOfficialVersion] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(5);
  const [query, setQuery] = useState("");
  const [versions, setVersions] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<Profile | null>(null);
  const [active, setActive] = useState("GPQA");
  const [params, setParams] = useState<Record<string, Param>>({});
  const [versionName, setVersionName] = useState("");
  const [saveTo, setSaveTo] = useState("新建配置");
  const [saving, setSaving] = useState(false);
  const [add, setAdd] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);
  const [catalog, setCatalog] = useState("");
  const [catalogMode, setCatalogMode] = useState("全量");
  const standardVersions = Array.from(new Set(
    profiles.filter((p) => p.official).flatMap((p) => p.versions),
  ));
  const currentOfficialVersion = standardVersions.includes(officialVersion)
    ? officialVersion
    : standardVersions[0] || "";
  const officialRows = profiles.filter((p) => p.official && p.versions.includes(currentOfficialVersion));
  const filtered = (kind === "历史标准配置" ? officialRows : profiles.filter((p) => !p.official))
    .filter((p) => [p.name, p.source, p.model, p.modelId].join(" ").toLowerCase().includes(query.toLowerCase()));
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  useEffect(() => setPage(1), [kind, currentOfficialVersion, query]);

  const open = (p: Profile) => {
    setEditing({ ...p, items: [...p.items] });
    setActive(p.items[0] || "GPQA");
    const selectedVersion = p.official ? currentOfficialVersion : versions[p.id] || p.versions[0];
    setParams(structuredClone(p.data?.[selectedVersion]) || Object.fromEntries(p.items.map((d, i) => [d, base(i)])));
    setVersionName("");
    setSaveTo("新建配置");
  };

  const configurationTabs = <div className="tabs configuration-tabs" role="tablist" aria-label="配置类型">
    {["标准配置", "自定义配置"].map((name) =>
      <button type="button" role="tab" aria-selected={name === "标准配置" ? kind !== "自定义配置" : kind === name} className={(name === "标准配置" ? kind !== "自定义配置" : kind === name) ? "active" : ""} key={name} onClick={() => setKind(name)}>{name}</button>)}
  </div>;
  if (kind === "标准配置") return <section className="card stack configuration-panel">
    {configurationTabs}
    <div className="spread"><p className="muted">标准配置只读。在详情中查看标准参数与版本来源。</p><button className="link" onClick={() => setKind("历史标准配置")}>查看历史标准配置</button></div>
    <StandardInputs />
  </section>;

  return (
    <div className="stack">
      <section className="card configuration-panel">{configurationTabs}</section>
      {kind === "历史标准配置" && <div className="spread"><h3>历史标准配置</h3><button className="link" onClick={() => setKind("标准配置")}>返回当前标准配置</button></div>}
      <Note>
        标准配置只读；自定义配置保存为新版本，历史版本保持不变。
      </Note>
      <div className="config-toolbar">
        <input aria-label="搜索配置" placeholder="搜索配置名称或来源" value={query} onChange={(e) => setQuery(e.target.value)} />
        <button className="primary" onClick={() => open({
          id: "new", name: "新配置", source: "未保存", official: false, versions: [], items: ["GPQA"],
        })}>新建配置</button>
        <button onClick={() => notify("导入格式、校验与失败恢复规则待确认，暂未开放文件导入。")}>导入配置</button>
      </div>
      <section className="card stack">
        <div className="config-list-heading">
          {kind === "历史标准配置" && (
            <label className="official-version">
              仓库版本
              <select aria-label="标准配置版本" value={currentOfficialVersion} onChange={(e) => setOfficialVersion(e.target.value)}>
                {standardVersions.map((v) => <option key={v}>{v}</option>)}
              </select>
            </label>
          )}
        </div>
        <p className="muted">
          {kind === "历史标准配置"
            ? `当前版本 ${currentOfficialVersion || "无"} · 后端返回 ${officialRows.length} 个配置 · 仅供查看`
            : "自定义配置可保存多个版本，版本名称由维护者定义。"}
        </p>
        <div className="table-wrap">
          <table>
            <thead><tr>{[
              "配置名称", "模型标识", "配置来源", ...(kind === "自定义配置" ? ["配置版本"] : []), "数据集", "操作",
            ].map((h) => <th key={h}>{h}</th>)}</tr></thead>
            <tbody>
              {filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize).map((p) => (
                <tr key={p.id}>
                  <td>{p.name}</td>
                  <td>{p.modelId || "未关联模型"}</td>
                  <td>{p.source}</td>
                  {kind === "自定义配置" && (
                    <td>
                      <select aria-label={`${p.name}版本`} value={versions[p.id] || p.versions[0]} onChange={(e) => setVersions({ ...versions, [p.id]: e.target.value })}>
                        {p.versions.map((v) => <option key={v}>{v}</option>)}
                      </select>
                    </td>
                  )}
                  <td>{p.items.length} 个数据集</td>
                  <td><button className="link" onClick={() => open(p)}>{p.official ? "查看" : "维护配置"}</button></td>
                </tr>
              ))}
            </tbody>
          </table>
          {!filtered.length && <div className="empty">暂无匹配的配置</div>}
        </div>
        <div className="pagination" aria-label="配置列表分页">
          <span>共 {filtered.length} 条 · 每页 {pageSize} 条</span>
          <label className="pagination-page-size"><span>每页</span><select aria-label="每页条数" value={pageSize} onChange={e=>{setPageSize(Number(e.target.value));setPage(1);}}>{[5,15,30,50].map(size=><option key={size} value={size}>{size} 条</option>)}</select></label>
          <button disabled={currentPage === 1} onClick={() => setPage(currentPage - 1)}>上一页</button>
          <PageNumbers current={currentPage} total={pageCount} onChange={setPage} />
          <button disabled={currentPage === pageCount} onClick={() => setPage(currentPage + 1)}>下一页</button>
        </div>
      </section>
      {editing && (
        <ConfigurationDrawer>
          <div className="stack standard-detail custom-config-detail">
            <div className="spread">
              <h2>{editing.official ? "查看标准配置（只读）" : "维护自定义配置"}</h2>
              <button onClick={() => setEditing(null)}>关闭</button>
            </div>
            <div className="inline">
              <span>来源：{editing.source}</span>
              {editing.model && <span>模型：{editing.model} · {editing.modelId}</span>}
              <span>当前版本：{editing.official ? currentOfficialVersion : versions[editing.id] || editing.versions[0] || "未保存"}</span>
            </div>
            {editing.official && <details className="notice"><summary>标准来源与只读参数</summary>{(() => { const source = editing.provenance?.[currentOfficialVersion]; return source?.url ? <><a href={source.url} target="_blank" rel="noreferrer">查看批准配置来源</a><p>来源版本：{source.revision}</p><small>SHA-256：{source.sha256}</small></> : <p>此历史配置未提供可展示的来源元数据。</p>; })()}<p>本页标准参数只读。新同步的标准仓输入需在“标准仓输入与版本”中独立评审，不会替代当前配置。</p><pre className="snapshot">{JSON.stringify(params, null, 2)}</pre></details>}
            <div className="workflow-grid">
              <aside className="workflow-datasets">
                <DatasetTree
                  items={editing.items}
                  selected={editing.items}
                  active={active}
                  onActive={setActive}
                  onSelect={editing.official ? undefined : (items) => {
                    setEditing({ ...editing, items });
                    if (!items.includes(active)) setActive(items[0] || "");
                  }}
                />
                {!editing.official && <button onClick={() => { setPicked([]); setAdd(true); }}>添加数据集</button>}
              </aside>
              <div className="card stack custom-parameter-panel"><h3>{active}</h3>{params[active]?.adapter_input ? <details><summary>旧版兼容参数</summary><ParameterCards items={editing.items} active={active} params={{ ...params, [active]: { ...params[active], adapter_input: undefined } }} onChange={(next) => setParams({ ...next, [active]: { ...next[active], adapter_input: params[active].adapter_input } })} readonly={editing.official} /></details> : <ParameterCards items={editing.items} active={active} params={params} onChange={setParams} readonly={editing.official} />}
              {!editing.official && active && <CustomAdapterEditor dataset={active} value={params[active]?.adapter_input} onChange={(next) => { const updated = { ...(params[active] || base()) }; if (next) updated.adapter_input = next; else delete updated.adapter_input; setParams({ ...params, [active]: updated }); }} />}</div>
            </div>
            {!editing.official && (
              <div className="config-save-panel">
                <h3>保存设置</h3>
                <div className="config-save-fields">
                  <label>
                    <span>保存方式</span>
                    <select value={saveTo} onChange={(e) => setSaveTo(e.target.value)}>
                      <option>新建配置</option>
                      {profiles.filter((p) => !p.official).map((p) => <option key={p.id} value={p.id}>保存到：{p.name}</option>)}
                    </select>
                  </label>
                  <label>
                    <span>自定义版本名称 <span className="required-mark">*</span></span>
                    <input placeholder="例如：多模态验证-第三版" required aria-describedby="config-save-help" value={versionName} onChange={(e) => setVersionName(e.target.value)} />
                  </label>
                </div>
                <p id="config-save-help" className="config-save-help">
                  {saveTo === "新建配置" ? "创建一份独立配置，版本名称同时作为配置名称。" : "将本次参数保存为所选配置的新版本，保留已有历史版本。"}
                  {" "}保存到后端数据库；失败时保留当前编辑内容。
                </p>
                <div className="config-save-actions">
                  <button onClick={() => setEditing(null)}>取消</button>
                  <button
                    className="primary"
                    disabled={saving || !versionName.trim() || !editing.items.length}
                    onClick={async () => {
                      if (saving) return;
                      const invalid = document.querySelector<HTMLInputElement>(".adapter-fields :invalid");
                      if (invalid) { invalid.reportValidity(); return; }
                      const target = saveTo === "新建配置" ? null : profiles.find((p) => p.id === saveTo);
                      if (saveTo !== "新建配置" && !target) { notify("目标配置不存在，请刷新"); return; }
                      const key = target?.id || `custom-${crypto.randomUUID()}`;
                      const items = Object.fromEntries(editing.items.map((name) => [name, params[name]]));
                      setSaving(true);
                      try {
                        const response = await fetch("/api/autoeval/v1/test-configs", {
                          method: "POST",
                          headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({
                            config_key: key,
                            display_name: target?.name || versionName.trim(),
                            version_label: versionName.trim(),
                            expected_version: target?.persistedVersion || 0,
                            items,
                          }),
                        });
                        const body = await response.json();
                        if (!response.ok) throw new Error(body.error?.message || "保存失败");
                        const saved: Profile = {
                          ...editing,
                          id: key,
                          name: target?.name || versionName.trim(),
                          official: false,
                          source: "后端持久化",
                          persistedVersion: body.data.version,
                          versions: [versionName.trim(), ...(target?.versions || [])],
                          items: [...editing.items],
                          data: { ...target?.data, [versionName.trim()]: structuredClone(items) },
                        };
                        setProfiles(target ? profiles.map((p) => p.id === key ? saved : p) : [...profiles, saved]);
                        setEditing(null);
                        notify("配置已保存到后端，刷新后可重新加载。");
                      } catch (error) {
                        notify(error instanceof Error ? error.message : "后端不可用，配置未保存");
                      } finally { setSaving(false); }
                    }}
                  >保存配置</button>
                </div>
              </div>
            )}
          </div>
        </ConfigurationDrawer>
      )}
      {add && (
        <div className="dataset-overlay">
          <section className="dataset-dialog">
            <h3>添加数据集</h3>
            <input placeholder="搜索数据集" value={catalog} onChange={(e) => setCatalog(e.target.value)} />
            <p className="muted">{catalogError || "仅显示标准仓已登记的 AISBench 数据集，展示不代表已开放执行。"}</p><select value={catalogMode} onChange={(e) => setCatalogMode(e.target.value)}><option>全量</option><option>Lite</option></select>
            <DatasetTree
              items={catalogError ? [] : catalogNames.filter((d) => d.toLowerCase().includes(catalog.toLowerCase()))}
              selected={picked}
              disabled={editing?.items || []}
              onSelect={setPicked}
              scope={catalogMode}
            />
            <Note>重复项暂禁用以避免重复；正式去重规则待确认。</Note>
            <div className="inline">
              <button onClick={() => setAdd(false)}>取消</button>
              <button className="primary" disabled={!picked.length} onClick={() => {
                if (editing) {
                  setEditing({ ...editing, items: [...editing.items, ...picked] });
                  setParams({ ...params, ...Object.fromEntries(picked.map((d) => [d, { ...base(), scope: catalogMode }])) });
                }
                setAdd(false);
              }}>确认添加</button>
            </div>
          </section>
        </div>
      )}
      <button className="link back" onClick={() => go("console")}>返回控制台</button>
    </div>
  );
}

export function EvaluationConsole({ profiles, setProfiles, notify }: {
  profiles: Profile[];
  setProfiles: (p: Profile[]) => void;
  notify: (s: string) => void;
}) {
  const configs = useOptions<Config>("/test-configs");
  const targets = useOptions<Target>("/target-bindings");
  const docker = useOptions<Resource>("/resources");
  const ssh = useOptions<Resource>("/ssh-resources");
  const monitored = useOptions<Resource>("/aisbench-monitored/resources");
  const [target, setTarget] = useState<Target | null>(null);
  const [resource, setResource] = useState<Resource | null>(null);
  const [resourceKind, setResourceKind] = useState("docker_local");
  const resources = resourceKind === "aisbench_monitor" ? monitored : resourceKind === "customer_ssh" ? ssh : docker;
  const { config, setConfig, loadingDefault, defaultError, setDefaultError, reloadDefault } = useDefaultConfig(target);
  const { busy, message, submit } = useExplorationSubmission();
  const [configKind, setConfigKind] = useState("标准配置");
  const [datasetSearch, setDatasetSearch] = useState("");
  const [view, setView] = useState<string | null>(null);
  const [drawer, setDrawer] = useState(false);
  const [draftParams, setDraftParams] = useState<Record<string, Param>>({});
  const [newConfigName, setNewConfigName] = useState("");
  const [newVersionName, setNewVersionName] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (config) setConfigKind(config.kind === "custom" ? "自定义配置" : "标准配置");
  }, [config]);

  const targetRows = withSelected(targets.rows.filter(eligibleTarget), target);
  const configRows = withSelected(
    configs.rows.filter(eligibleConfig).filter((row) => configKind === "自定义配置" ? row.kind === "custom" : row.kind === "standard"),
    config,
  );
  const resourceRows = withSelected(
    resources.rows.filter((row) => resourceKind !== "customer_ssh" || row.native_tool_root),
    resource,
  );
  const visibleDatasets = names.filter((d) => d.toLowerCase().includes(datasetSearch.toLowerCase()));
  const viewParams = view ? config?.items[view] : undefined;
  const openEditor = () => {
    if (!config || config.kind !== "custom") return;
    setDraftParams(structuredClone(config.items));
    setNewConfigName("");
    setNewVersionName("");
    setDrawer(true);
  };

  return (
    <div className="console-grid console-workflow">
      <div className="console-configuration">
        <section className="console-step">
          <div className="console-section-heading"><h3 className="console-step-title">模型接入</h3><div className="management-toolbar"><ManagementDrawer title="管理模型服务" onDismiss={targets.retry}><ServiceBindings /></ManagementDrawer><ManagementDrawer title="管理执行资源" onDismiss={resources.retry}><SSHResources /></ManagementDrawer></div></div>
          <div className="tabs">
            <button className="active">API接入</button>
            <button disabled title="当前探索路径不支持 IP/Port 拉起">IP/Port 拉起</button>
          </div>
          <div className="stack">
            <label>模型服务
              <SearchableSelect aria-label="控制台模型服务" value={target?.id || ""} disabled={busy || targets.loading} onChange={(e) => setTarget(targetRows.find((row) => row.id === e.target.value) || null)}>
                <option value="">请选择官方 DeepSeek 服务</option>
                {targetRows.map((row) => <option key={row.id} value={row.id}>{row.model_key} / {row.model_version} · {row.remote_model} · {row.id.slice(0, 8)}</option>)}
              </SearchableSelect>
            </label>
            <label>资源接入方式
              <select aria-label="控制台资源接入方式" value={resourceKind} disabled={busy} onChange={(e) => { setResourceKind(e.target.value); setResource(null); }}>
                <option value="docker_local">本机Docker</option>
                <option value="customer_ssh">客户SSH服务器</option><option value="aisbench_monitor">远程 AISBench（运行监控）</option>
              </select>
            </label>
            <label>执行资源
              <SearchableSelect aria-label="控制台执行资源" value={resource?.id || ""} disabled={busy || resources.loading} onChange={(e) => setResource(resourceRows.find((row) => row.id === e.target.value) || null)}>
                <option value="">请选择已保存资源</option>
                {resourceRows.map((row) => <option key={row.id} value={row.id}>{row.binding_key}</option>)}
              </SearchableSelect>
            </label>
          </div>
        </section>
        <section className="console-step stack">
          <div className="console-section-heading"><h3 className="console-step-title">测评配置</h3>          <ManagementDrawer title="模型与默认配置" onDismiss={() => { targets.retry(); configs.retry(); }}><ModelRegistry onApply={(_id, _values, configKey, versionLabel) => {
            const match = configs.rows.find((row) => row.config_key === configKey && row.version_label === versionLabel && eligibleConfig(row));
            if (match) setConfig(match);
            else notify("默认配置已读取；请选择已保存服务，按不可变配置版本ID加载探索参数。");
          }} /></ManagementDrawer>
</div>
          <div className="console-config-fields">
            <fieldset className="console-config-type">
              <legend>配置类型</legend>
              <div className="console-config-segments">
                {["标准配置", "自定义配置"].map((item) => (
                  <label key={item} className={configKind === item ? "selected" : ""}>
                    <input
                      type="radio"
                      name="console-config-type"
                      checked={configKind === item}
                      onChange={() => { setConfigKind(item); setConfig(null); setDefaultError(""); }}
                    />{item}
                  </label>
                ))}
              </div>
            </fieldset>
            <label className="evaluation-version-field">
              <span>配置版本</span>
              <SearchableSelect aria-label="控制台测评配置" value={config?.id || ""} disabled={busy || loadingDefault} onChange={(e) => {
                setConfig(configRows.find((row) => row.id === e.target.value) || null);
                setDefaultError("");
              }}>
                <option value="">请选择已绑定 Profile 的 GPQA Lite 配置</option>
                {configRows.map((row) => <option key={row.id} value={row.id}>{row.display_name} · {row.version_label}</option>)}
              </SearchableSelect>
            </label>
          </div>
          {loadingDefault && <p role="status">正在按配置版本ID读取模型默认配置…</p>}
          {defaultError && <p role="status">{defaultError}</p>}
          <div className="console-config-actions"><button disabled={busy || !target || loadingDefault} onClick={reloadDefault}>加载模型默认配置</button>{configKind === "自定义配置" && <button disabled={config?.kind !== "custom"} onClick={openEditor}>编辑配置</button>}</div>
          {[configs, targets, resources].some((source) => source.error) && (
            <p role="alert">选项加载失败。<button onClick={() => { configs.retry(); targets.retry(); resources.retry(); }}>重试选项加载</button></p>
          )}
          <Note>配置须为单 GPQA 数据集、Lite 且已绑定 Profile；提交时后台再次校验，未绑定的配置不会出现在选项中。</Note>

        </section>
        <section className="console-step">
          <h3 className="console-step-title">测评数据集</h3>
          <div className="dataset-selection-toolbar">
            <input aria-label="搜索可选数据集" placeholder="搜索数据集" value={datasetSearch} onChange={(e) => setDatasetSearch(e.target.value)} />
            <span>已选 {config ? 1 : 0} 个</span>
          </div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>选择</th><th>数据集类型</th><th>数据集</th><th>数据范围</th><th>操作</th></tr></thead>
              <tbody>
                {visibleDatasets.map((d) => (
                  <tr key={d}>
                    <td><input type="checkbox" aria-label={`选择 ${d}`} checked={d === "GPQA" && Boolean(config)} disabled readOnly /></td>
                    <td>{datasetTypes.find((item) => item.items.includes(d))?.label}</td>
                    <td>{d}</td>
                    <td>{d === "GPQA" ? config?.items.GPQA.scope || "需选择配置" : "当前路径未开放"}</td>
                    <td><div className="inline">
                      {d === "GPQA" && <><button className="link" disabled={!config} onClick={() => setView(d)}>查看参数</button>
                      <button className="link" disabled={d !== "GPQA" || config?.kind !== "custom"} onClick={openEditor}>编辑参数</button></>}
                    </div></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!visibleDatasets.length && <div className="empty">没有匹配的数据集</div>}
          <Note>当前仅开放 GPQA Lite 单数据集串行探索；MMMU-Pro、AutomationBench、全量及并行执行均不可提交。</Note>
        </section>
      </div>
      <section className="card stack summary">
        <h2>任务摘要</h2>
        <h3>{target ? `${target.model_key} / ${target.model_version}` : "未选择模型服务"}</h3>
        <span>模型标识：{target?.model_key || "未选择"}</span>
        <span>配置：{config ? `${config.display_name} · ${config.version_label}` : "未选择"}</span>
        <dl className="planning-checklist" aria-label="提交前检查">
          <div><dt>模型服务</dt><dd>{target ? "已选择" : "待选择"}</dd></div>
          <div><dt>执行资源</dt><dd>{resource ? "已选择" : "待选择"}</dd></div>
          <div><dt>数据集配置</dt><dd>{config ? "GPQA Lite · 已配置" : "待配置"}</dd></div>
        </dl>
        <fieldset className="run-mode-options">
          <legend>运行方式</legend>
          <div className="run-mode-choices">
            <label className="is-selected"><input type="radio" name="evaluation-run-mode" value="串行" checked readOnly /><span>串行</span></label>
            <label><input type="radio" name="evaluation-run-mode" value="并行" disabled readOnly /><span>并行</span></label>
          </div>
          <p className="run-mode-description">单数据集串行执行；并行与 Release 发布流程不在当前探索路径内。</p>
        </fieldset>
        <section className="summary-scope">
          <div className="spread"><h3>测评范围</h3><strong>{config ? 1 : 0} 个数据集</strong></div>
          <p>Lite {config ? 1 : 0} · 最多 5 次模型请求</p>
          <details>
            <summary>展开选择明细</summary>
            <div className="summary-dataset-list">
              {config ? <div><div className="spread"><b>GPQA</b><span>{config.items.GPQA.scope}</span></div><small>预计耗时：暂无可靠估算</small></div> : <p>尚未选择配置</p>}
            </div>
          </details>
        </section>
        <div className="total-duration" role="status" aria-live="polite">
          <div className="total-duration-heading"><span>测评预计总耗时</span><span className="badge">串行 · {config ? 1 : 0} 个数据集</span></div>
          <strong>暂无可靠估算</strong>
          <p>不含排队及报告分析耗时。</p>
        </div>
        <div className="console-submit-status" role="status">
          {message || (!target ? "请选择已保存的 DeepSeek 服务。" : !config ? "请选择符合探索条件的配置。" : !resource ? "请选择执行资源。" : "提交将创建真实测评任务，可能产生模型调用费用。")}
        </div>
        <SubmissionReview config={loadingDefault ? null : config} target={target} resource={resource} resourceKind={resourceKind} busy={busy} submit={() => void submit(config, target, resource, resourceKind)} />
      </section>
      {view && viewParams && (
        <div className="dataset-overlay" onClick={() => setView(null)}>
          <section className="dataset-dialog readable-config-dialog" role="dialog" aria-modal="true" aria-label={`${view} 数据集参数`} onClick={(e) => e.stopPropagation()}>
            <div className="spread">
              <div><h3>{view} 数据集参数</h3><p className="muted">来自已保存配置版本，仅供查看。</p></div>
              <span className="badge neutral">只读</span>
            </div>
            <div className="readable-config-scroll">
              <dl className="readable-config-list">
                {[
                  ["测评范围", viewParams.scope, "使用配置绑定的 Lite 清单。"],
                  ["回答长度上限", `${viewParams.output.toLocaleString()} Token`, "限制每次回答的生成长度。"],
                  ["同时处理数量", `${viewParams.concurrency} 个请求`, "后台将按探索路径再次校验。"],
                  ["回答随机性", viewParams.temperature, "生成参数来自所选配置版本。"],
                  ["候选内容范围", viewParams.top_p, "生成参数来自所选配置版本。"],
                ].map(([label, value, help]) => <div key={label}><dt>{label}</dt><dd><strong>{value}</strong><p>{help}</p></dd></div>)}
              </dl>
              <details className="raw-config-details"><summary>查看原始配置（JSON）</summary><pre className="snapshot">{JSON.stringify(viewParams, null, 2)}</pre></details>
            </div>
            <div className="readable-config-footer"><button autoFocus onClick={() => setView(null)}>关闭</button></div>
          </section>
        </div>
      )}
      {drawer && config?.kind === "custom" && (
        <ConfigurationDrawer>
          <h2>自定义配置</h2>
          <div className="workflow-grid">
            <aside className="workflow-datasets"><DatasetTree items={["GPQA"]} selected={["GPQA"]} active="GPQA" /></aside>
            <ParameterCards items={["GPQA"]} active="GPQA" params={draftParams} onChange={setDraftParams} />
          </div>
          <div className="config-save-panel">
            <h3>保存为新配置</h3>
            <div className="config-save-fields">
              <label><span>配置名称 *</span><input aria-label="新配置名称" required value={newConfigName} onChange={(e) => setNewConfigName(e.target.value)} placeholder="例如：GPQA Lite 专项验证" /></label>
              <label><span>版本名称 *</span><input aria-label="新配置版本名称" required value={newVersionName} onChange={(e) => setNewVersionName(e.target.value)} placeholder="例如：初版" /></label>
            </div>
            <p className="config-save-help">测评只能引用后端已保存的配置版本；保存会保留当前 Profile 绑定，符合探索条件的新版本将自动选用。</p>
          </div>
          <div className="actions">
            <button onClick={() => setDrawer(false)}>取消</button>
            <button
              disabled={saving || !newConfigName.trim() || !newVersionName.trim() || !draftParams.GPQA}
              className="primary"
              onClick={async () => {
                if (saving || !config) return;
                const name = newConfigName.trim();
                const version = newVersionName.trim();
                const key = `custom-${crypto.randomUUID()}`;
                const items = { GPQA: draftParams.GPQA };
                setSaving(true);
                try {
                  const response = await fetch("/api/autoeval/v1/test-configs", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                      config_key: key,
                      display_name: name,
                      version_label: version,
                      expected_version: 0,
                      items,
                      profile_bindings: { GPQA: config.profile_bindings.GPQA },
                    }),
                  });
                  const body = await response.json();
                  if (!response.ok) throw new Error(body.error?.message || "配置保存失败");
                  const saved: Config = {
                    id: body.data.id,
                    config_key: key,
                    display_name: name,
                    version: body.data.version,
                    version_label: version,
                    kind: "custom",
                    items,
                    profile_bindings: config.profile_bindings,
                  };
                  setProfiles([...profiles, {
                    id: key,
                    name,
                    source: "后端持久化",
                    official: false,
                    persistedVersion: body.data.version,
                    versions: [version],
                    items: ["GPQA"],
                    data: { [version]: structuredClone(items) },
                  }]);
                  configs.retry();
                  if (eligibleConfig(saved)) {
                    setConfig(saved);
                    notify("新配置已保存到后端并自动选用。");
                  } else {
                    setConfig(null);
                    notify("新配置已保存，但不符合 GPQA Lite 探索条件，未自动选用。");
                  }
                  setDrawer(false);
                } catch (error) {
                  notify(error instanceof Error ? error.message : "配置未保存");
                } finally { setSaving(false); }
              }}
            >保存新配置并返回</button>
          </div>
        </ConfigurationDrawer>
      )}
    </div>
  );
}
