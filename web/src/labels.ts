import type { AgentName, StreamEvent } from "./api/types";

export interface TraceItem {
  tag: string;
  text: string;
  tone: "normal" | "warn" | "veto";
}

/** The five agents of the spec, in order. */
export const AGENTS: Record<AgentName, string> = {
  request_analyst: "סוכן 1 · ניתוח הבקשה",
  catalog_search: "סוכן 2 · חיפוש במאגר (PostGIS + RAG)",
  enrichment: "סוכן 3 · העשרת מידע ובדיקת בטיחות",
  route_planner: "סוכן 4 · תכנון המסלול",
  presenter: "סוכן 5 · הצגת התוצאה",
};

const hhmm = (t: string | null | undefined): string => (t ? t.slice(0, 5) : "?");

/** Turns a stream event into one line of the "what the agents are doing" panel (or nothing). */
export function describeEvent(ev: StreamEvent): TraceItem | null {
  const item = (tag: string, text: string, tone: TraceItem["tone"] = "normal"): TraceItem => ({ tag, text, tone });
  switch (ev.type) {
    case "node": {
      const round = ev.data.round && ev.data.round > 1 ? ` · סבב ${ev.data.round} (חיפוש רחב יותר)` : "";
      return item(ev.data.node, AGENTS[ev.data.node] + round);
    }
    case "request": {
      const r = ev.data.request as { region?: string; trip_date?: string; difficulty?: string; trail_types?: string[] };
      const parts = [r.region, r.trip_date, r.difficulty, ...(r.trail_types ?? [])].filter(Boolean);
      return item("request", `הבנתי: ${parts.join(" · ")}`);
    }
    case "tool_calls":
      return item("tools", `במקביל: ${ev.data.calls.map((c) => c.tool).join(" + ")}`);
    case "candidates":
      return item(
        "candidates",
        ev.data.trails.length
          ? `${ev.data.trails.map((t) => `${t.name} (${t.km.toFixed(1)} ק"מ)`).join(" · ")} · ${ev.data.chunks} קטעי ידע`
          : "לא נמצאו מועמדים",
      );
    case "assessment": {
      const a = ev.data.assessment;
      const name = a.trail_name ?? a.trail_id;
      if (!a.approved) {
        const why = a.issues.filter((i) => i.severity === "critical").map((i) => i.evidence).join("; ");
        return item("veto", `${name} נפסל: ${why}`, "veto");
      }
      const w = a.weather;
      const weather =
        w && (w.status === "ok" || w.status === "cached")
          ? `${w.temp_max_c ?? "?"}°C, גשם ${w.precip_prob_max_pct ?? "?"}%`
          : "אין תחזית";
      const warn = a.issues.filter((i) => i.severity !== "critical").length;
      return item("safe", `${name} אושר · ${weather} · יציאה ${hhmm(a.start_time)}${warn ? ` · הערות: ${warn}` : ""}`, warn ? "warn" : "normal");
    }
    case "plan":
      return item("plan", `${ev.data.plan.title} · ${hhmm(ev.data.plan.start_time)}-${hhmm(ev.data.plan.end_time)}`);
    case "timing":
      return item("time", `${AGENTS[ev.data.node]}: ${ev.data.seconds.toFixed(1)} שניות`);
    case "done": {
      const ms = ev.data.tool_calls.reduce((sum, c) => sum + c.ms, 0);
      return item("done", `${ev.data.tool_calls.length} קריאות כלים · ${ms}ms`);
    }
    default:
      return null;
  }
}
