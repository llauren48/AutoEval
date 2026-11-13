import { apiGetPage } from "./api";

export type StandardCatalogEntry = {
  source_model: string; benchmark: string; revision: string;
  files: Record<string, { raw: string }>;
};

// Source directory names are authoritative. No guessed ModelScope ID or variant alias.
export async function standardCatalog(signal?: AbortSignal): Promise<StandardCatalogEntry[]> {
  const result = new Map<string, StandardCatalogEntry>();
  let offset: number | null = 0;
  while (offset !== null) {
    const page: { data: StandardCatalogEntry[]; next_offset: number | null } = await apiGetPage(`/standard-inputs?revision=all&offset=${offset}&limit=100`, signal);
    for (const row of page.data) {
      const key = `${row.source_model}/${row.benchmark}`;
      if (!result.has(key)) result.set(key, row);
    }
    if (page.next_offset !== null && page.next_offset <= offset) throw Error("标准目录分页异常");
    offset = page.next_offset;
  }
  return [...result.values()];
}

export async function aisbenchCatalog(signal?: AbortSignal) {
  return (await standardCatalog(signal)).filter((row) => {
    const raw = row.files["standard_config.json"]?.raw;
    if (!raw) return false;
    const config = JSON.parse(raw);
    return config.tool === "ais_bench" && ["llm_accuracy", "harbor_agent"].includes(config.subcommand);
  });
}
