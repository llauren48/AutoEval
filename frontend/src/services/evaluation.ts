import { apiGetOptions } from "./api";
import { useEffect, useRef, useState } from "react";
import { apiGet, apiPost } from "./api";
import { REGISTRY_PAGE_SIZE } from "../components/OffsetPagination";

export type Config = {
  source_url?: string | null; source_revision?: string | null; source_sha256?: string | null; approved_by?: string | null;
  id: string;
  config_key: string;
  display_name: string;
  version: number;
  version_label: string;
  kind: string;
  items: Record<string, { output: number; concurrency: number; temperature: number; top_p: number; scope: string }>;
  profile_bindings: Record<string, string>;
};
export type Target = {
  id: string;
  remote_model: string;
  base_url: string;
  model_key: string;
  model_version: string;
  default_config_version_id: string | null;
};
export type Resource = { id: string; binding_key: string; native_tool_root?: string | null };
type PendingSubmission = { hash: string; key: string };
const pendingStorageKey = "autoeval.pending-exploration.v1";

export const eligibleConfig = (row: Config) =>
  Object.keys(row.items).length === 1 && row.items.GPQA?.scope === "Lite" && row.profile_bindings?.GPQA;
export const eligibleTarget = (row: Target) =>
  row.remote_model === "deepseek-flash" && row.base_url.replace(/\/$/, "") === "https://api.deepseek.com";
export const runLink = (id: string) => `#run/${id}`;
export const withSelected = <T extends { id: string }>(rows: T[], selected: T | null) =>
  selected && !rows.some((row) => row.id === selected.id) ? [selected, ...rows] : rows;

function readPending(): PendingSubmission | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(pendingStorageKey) || "null");
    return value && typeof value.hash === "string" && value.hash.length < 4096 && typeof value.key === "string" && /^[0-9a-f-]{36}$/.test(value.key) ? value : null;
  } catch { return null; }
}

function storePending(value: PendingSubmission | null) {
  // Only UUIDs, mode and bounded request limits are in this hash; never credentials.
  try { if (value) sessionStorage.setItem(pendingStorageKey, JSON.stringify(value)); else sessionStorage.removeItem(pendingStorageKey); }
  catch { /* Restricted storage falls back to the in-memory intent. */ }
}

export function useOptions<T>(path: string) {
  const [offset, setOffset] = useState(0);
  const [rows, setRows] = useState<T[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(""); setRows([]); setNext(null);
    apiGetOptions<T[]>(`${path}?offset=${offset}&limit=${REGISTRY_PAGE_SIZE}`, controller.signal)
      .then((page) => { if (!controller.signal.aborted) { setRows(page.data); setNext(page.next_offset); } })
      .catch(() => { if (!controller.signal.aborted) setError("选项加载失败，请重试。"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [path, offset, refresh]);
  return { rows, offset, next, loading, error, setOffset, retry: () => setRefresh((value) => value + 1) };
}

export function useDefaultConfig(target: Target | null) {
  const [config, setConfig] = useState<Config | null>(null);
  const [loadingDefault, setLoadingDefault] = useState(false);
  const [defaultError, setDefaultError] = useState("");
  const [defaultRefresh, setDefaultRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setConfig(null); setDefaultError(""); setLoadingDefault(false);
    if (!target) return () => controller.abort();
    setLoadingDefault(true);
    apiGet<Target[]>(`/target-bindings?target_id=${target.id}`, controller.signal)
      .then((rows) => {
        if (!rows[0]?.default_config_version_id) { throw new Error("该模型尚未绑定默认配置，可显式选择探索配置。"); }
        return apiGet<Config[]>(`/test-configs?config_id=${rows[0].default_config_version_id}`, controller.signal);
      })
      .then((rows) => {
        if (controller.signal.aborted) return;
        if (!rows[0] || !eligibleConfig(rows[0])) { setDefaultError("模型默认配置不适用于当前GPQA Lite探索，请显式选择其他配置。"); return; }
        setConfig(rows[0]);
      }).catch((error) => { if (!controller.signal.aborted) setDefaultError(error instanceof Error && error.message.startsWith("该模型") ? error.message : "默认配置读取失败，可重试或显式选择配置。"); })
      .finally(() => { if (!controller.signal.aborted) setLoadingDefault(false); });
    return () => controller.abort();
  }, [target, defaultRefresh]);
  return {
    config, setConfig, loadingDefault, defaultError, setDefaultError,
    reloadDefault: () => setDefaultRefresh((value) => value + 1),
  };
}

export function useExplorationSubmission() {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const submitting = useRef(false);
  const [initialPending] = useState(readPending);
  const pending = useRef<PendingSubmission | null>(initialPending);

  const submit = async (config: Config | null, target: Target | null, resource: Resource | null, resourceKind: string) => {
    if (submitting.current || !config || !target || !resource) return;
    submitting.current = true; setBusy(true); setMessage("");
    const payload = { config_version_id: config.id, target_id: target.id, resource_id: resource.id, resource_kind: resourceKind, run_mode: "exploration", max_model_requests: 5 };
    const hash = JSON.stringify(payload);
    if (pending.current?.hash !== hash) pending.current = { hash, key: crypto.randomUUID() };
    const submission = pending.current;
    storePending(submission);
    try {
      const row = await apiPost<{ run_id: string }>(resourceKind === "aisbench_monitor" ? "/aisbench-monitored/runs" : "/runs", payload, submission.key);
      pending.current = null; storePending(null); location.hash = runLink(row.run_id); setMessage("已提交；按冻结的Lite清单执行。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "提交未确认；重试沿用同一提交键。"); }
    finally { submitting.current = false; setBusy(false); }
  };
  return { busy, setBusy, message, setMessage, submit };
}
