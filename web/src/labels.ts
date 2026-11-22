import type { StreamEvent } from "./api/types";

export interface TraceItem {
  tag: string;
  text: string;
  tone: "normal" | "warn" | "veto";
}

const NODE_LABELS: Record<string, string> = {
  load_profile: "טוען העדפות שמורות",
  supervisor: "מנתב את הבקשה",
  intake: "מחלץ פרמטרים",
  researcher: "מחפש מסלולים (מרחבי + סמנטי)",
  grader: "מדרג רלוונטיות",
  planner: "בונה מסלול",
  enrich: "בודק מזג אוויר, אור יום והתראות",
  safety_critic: "בדיקת בטיחות",
  safe_fallback: "אין חלופה בטוחה",
  explainer: "מנסח הסבר",
  ask_clarification: "מנסח שאלת הבהרה",
};

const hhmm = (t: string | null | undefined): string => (t ? t.slice(0, 5) : "?");

/** Turns a stream event into one line of the "what the agents are doing" panel (or nothing). */
export function describeEvent(ev: StreamEvent): TraceItem | null {
  const item = (tag: string, text: string, tone: TraceItem["tone"] = "normal"): TraceItem => ({ tag, text, tone });
  switch (ev.type) {
    case "node": {
      if (ev.data.status !== "start") return null;
      const label = NODE_LABELS[ev.data.node] ?? ev.data.node;
      const round =
        ev.data.node === "researcher" && ev.data.round
          ? ` · סבב ${ev.data.round}${ev.data.relaxed ? " (מסננים מורחבים)" : ""}`
          : "";
      return item(ev.data.node, label + round);
    }
    case "route":
      return item("route", `כוונה: ${ev.data.intent}`);
    case "candidates":
      return item(
        "candidates",
        ev.data.trails.length ? ev.data.trails.map((t) => `${t.name} (${t.km.toFixed(1)} ק"מ)`).join(" · ") : "לא נמצאו מועמדים",
      );
    case "graded":
      return item("grader", `נשמרו ${ev.data.kept} מתוך ${ev.data.total} קטעי ידע`);
    case "plan":
      return item("plan", `${ev.data.plan.title} · ${hhmm(ev.data.plan.start_time)}-${hhmm(ev.data.plan.end_time)}`);
    case "conditions": {
      const w = ev.data.weather;
      const weather =
        w && (w.status === "ok" || w.status === "cached")
          ? `${w.temp_max_c ?? "?"}°C · גשם ${w.precip_prob_max_pct ?? "?"}% · שקיעה ${w.sunset ? w.sunset.slice(11, 16) : "?"}`
          : `תחזית: ${w ? w.status : "לא זמינה"}`;
      const open = ev.data.status ? (ev.data.status.is_open ? "פתוח" : "סגור") : "?";
      return item("enrich", `${weather} · מסלול ${open} · התראות: ${ev.data.alerts.length}`);
    }
    case "verdict": {
      const { approved, attempt, issues, scope } = ev.data;
      if (approved) {
        const warn = issues.length ? ` · אזהרות: ${issues.length}` : "";
        return item("safety", `אושר (ניסיון ${attempt})${warn}`, issues.length ? "warn" : "normal");
      }
      const why = issues.filter((i) => i.severity === "critical").map((i) => i.evidence).join("; ");
      return item("veto", `וטו (${scope === "timing" ? "שינוי שעה" : "מסלול חלופי"}): ${why}`, "veto");
    }
    case "warning":
      return item("warn", ev.data.detail, "warn");
    case "done": {
      const ms = ev.data.tool_calls.reduce((sum, c) => sum + c.ms, 0);
      return item("done", `${ev.data.tool_calls.length} קריאות כלים · ${ms}ms`);
    }
    default:
      return null;
  }
}
