import type { Health, PipelineStatus, TrailFeature } from "./types";

async function getJson<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, init);
  if (!resp.ok) throw new Error(`${url}: HTTP ${resp.status}`);
  return (await resp.json()) as T;
}

export const api = {
  health: () => getJson<Health>("/health"),
  pipeline: () => getJson<PipelineStatus>("/pipeline"),

  /** Cached: the map asks for the same trail several times (veto layer, chosen layer). */
  trail: (() => {
    const cache = new Map<string, Promise<TrailFeature | null>>();
    return (trailId: string): Promise<TrailFeature | null> => {
      let hit = cache.get(trailId);
      if (!hit) {
        hit = getJson<TrailFeature>(`/trails/${encodeURIComponent(trailId)}/geojson`).catch(() => null);
        cache.set(trailId, hit);
      }
      return hit;
    };
  })(),

  // Demo only: the Mock Alerts API scenarios. Removed once real safety data is wired in.
  scenarios: () => getJson<{ scenarios: Record<string, string>; current: string }>("/scenarios"),
  setScenario: (name: string) =>
    getJson<{ scenario: string }>(`/scenarios/${encodeURIComponent(name)}`, { method: "POST" }),
};
