export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

export async function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  return (await apiGetPage<T>(path, signal)).data;
}

export async function apiGetPage<T>(path: string, signal?: AbortSignal): Promise<{ data: T; next_offset: number | null; total?: number | null }> {
  const response = await fetch(`/api/autoeval/v1${path}`, {
    signal,
    headers: { Accept: "application/json" },
  });
  const body = await response.json();
  if (!response.ok) {
    throw new ApiError(response.status, body.error?.code || "HTTP_ERROR", body.error?.message || "请求失败");
  }
  return { data: body.data as T, next_offset: body.next_offset ?? null, total: body.total ?? null };
}

export async function apiPost<T>(path: string, payload?: unknown, key?: string): Promise<T> {
  const response = await fetch(`/api/autoeval/v1${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(key ? { "Idempotency-Key": key } : {}) },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  const body = await response.json();
  if (!response.ok) throw new ApiError(response.status, body.error?.code || "HTTP_ERROR", body.error?.message || "请求失败");
  return body.data as T;
}

export interface BackendCapabilities {
  workspace_mode: "single";
  subject: string;
  evaluation_enabled: boolean;
  analysis_enabled: boolean;
  customer_ssh_enabled: boolean;
}

// Small registry option sets are fetched across all pages so search is complete.
export async function apiGetOptions<T>(path: string, signal?: AbortSignal): Promise<{ data: T; next_offset: null }> {
  const url = new URL(path, "http://local");
  const rows: unknown[] = []; let offset: number | null = 0;
  while (offset !== null) {
    url.searchParams.set("offset", String(offset)); url.searchParams.set("limit", "100");
    const page: { data: unknown[]; next_offset: number | null } = await apiGetPage(url.pathname + url.search, signal);
    rows.push(...page.data);
    if (page.next_offset !== null && page.next_offset <= offset) throw Error("选项分页异常");
    offset = page.next_offset;
  }
  return { data: rows as T, next_offset: null };
}
