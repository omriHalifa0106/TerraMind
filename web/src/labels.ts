import type { AgentName, StopKind, StreamEvent, TripRequest } from "./api/types";

export interface TraceItem {
  tag: string;
  text: string;
  tone: "normal" | "warn" | "veto";
}

/** The five agents of the spec, in pipeline order. */
export const AGENT_ORDER: AgentName[] = ["request_analyst", "catalog_search", "enrichment", "route_planner", "presenter"];

interface AgentInfo {
  persona: string; // short character name shown under the avatar
  title: string;
  role: string; // what it does, in one sentence (for people new to the system)
  working: string; // live status line while it runs
  engine: string; // what actually does the work
  tools: string[]; // MCP tools it calls (matches graph/nodes/*.py)
}

export const AGENT_INFO: Record<AgentName, AgentInfo> = {
  request_analyst: {
    persona: "המנתח",
    title: "ניתוח הבקשה",
    role: "קורא את הטקסט החופשי והופך אותו לפרמטרים מובנים: אזור, תאריך, קושי, סוג מסלול והרכב הקבוצה.",
    working: "קורא את הבקשה ומחלץ פרמטרים…",
    engine: "Claude · פלט מובנה",
    tools: ["find_trails_by_name"],
  },
  catalog_search: {
    persona: "החוקר",
    title: "חיפוש במאגר",
    role: "Claude בוחר כלים, ושני חיפושים רצים במקביל: חיפוש מרחבי ב-PostGIS וחיפוש סמנטי (RAG) בידע של מטיילים.",
    working: "מריץ שני חיפושים במקביל…",
    engine: "Claude tool calling · PostGIS + pgvector",
    tools: ["search_trails", "hybrid_search"],
  },
  enrichment: {
    persona: "השומר",
    title: "בטיחות ומזג אוויר",
    role: "בודק לכל מסלול תחזית, התראות וסגירות. כללים קבועים בקוד, ולא ה-LLM, פוסלים מסלול מסוכן לפני שמתכננים אותו.",
    working: "בודק תחזית, התראות וסגירות לכל מסלול…",
    engine: "כללי בטיחות דטרמיניסטיים · ללא LLM",
    tools: ["get_forecast", "get_trail_status", "list_active_alerts"],
  },
  route_planner: {
    persona: "המתכנן",
    title: "תכנון המסלול",
    role: "בוחר את המסלול הטוב מבין הבטוחים ואת המקומות ליום (מסעדה, יקב, עגלת קפה...), מסדר הכל בזמן, בודק שעות פתיחה, ומחשב נסיעות בכבישים אמיתיים.",
    working: "מסדר תחנות ומחשב זמני נסיעה והליכה…",
    engine: "Claude · פלט מובנה · PostGIS · OSRM",
    tools: ["get_trail_details", "find_pois_near_trail", "estimate_leg", "find_places_near"],
  },
  presenter: {
    persona: "המסביר",
    title: "הצגת התוצאה",
    role: "כותב את התשובה: המסלול, למה דווקא הוא, מה נפסל ולמה, וטיפים לשטח.",
    working: "כותב את התשובה…",
    engine: "Claude · סטרימינג",
    tools: [],
  },
};

/** Which agent called an MCP tool (by tool name; see AGENT_INFO.tools). */
export function toolOwner(tool: string): AgentName | null {
  return AGENT_ORDER.find((n) => AGENT_INFO[n].tools.includes(tool)) ?? null;
}

export const TOOL_LABEL: Record<string, string> = {
  search_trails: "PostGIS · חיפוש מרחבי",
  hybrid_search: "RAG · חיפוש סמנטי",
  find_trails_by_name: "PostGIS · חיפוש לפי שם",
};

export const STOP_KIND: Record<StopKind, string> = {
  parking: "חניה",
  trailhead: "תחילת השביל",
  poi: "נקודת עניין",
  viewpoint: "תצפית",
  water: "מים",
  food: "אוכל",
  end: "סיום",
  coffee: "קפה",
  attraction: "אטרקציה",
  winery: "יקב / מבשלה",
  farm: "חווה",
};

/** Hebrew names of day-plan place categories (gis.find_places_near). */
export const PLACE_LABEL: Record<string, string> = {
  restaurant: "מסעדה",
  cafe: "בית קפה",
  coffee_cart: "עגלת קפה",
  attraction: "אטרקציה",
  museum: "מוזיאון",
  winery: "יקב",
  brewery: "מבשלה",
  farm: "חווה",
  viewpoint: "תצפית",
  ruins: "אתר היסטורי",
  waterfall: "מפל",
};

const WORDS: Record<string, string> = {
  easy: "קל",
  moderate: "בינוני",
  medium: "בינוני",
  hard: "קשה",
  challenging: "מאתגר",
  water: "מים",
  family: "משפחות",
  view: "תצפית",
  viewpoint: "תצפית",
  loop: "מעגלי",
  forest: "יער",
  waterfall: "מפלים",
  history: "היסטוריה",
  low: "נמוך",
  high: "גבוה",
};
const word = (w: string): string => WORDS[w] ?? w;

/** Agent 1's output as short Hebrew chips (only the fields it actually filled). */
export function requestChips(r: TripRequest): { label: string; value: string }[] {
  const chips: { label: string; value: string }[] = [];
  const add = (label: string, value: string | null | false | undefined) => {
    if (value) chips.push({ label, value });
  };
  add("אזור", r.region);
  add("תאריך", r.trip_date && new Date(r.trip_date).toLocaleDateString("he-IL", { weekday: "long", day: "numeric", month: "numeric" }));
  add("יציאה", r.start_time && hhmm(r.start_time));
  add("קושי", r.difficulty && word(r.difficulty));
  add("סוג", r.trail_types.length > 0 && r.trail_types.map(word).join(", "));
  add("הליכה", r.max_duration_hours != null && `עד ${r.max_duration_hours} שעות`);
  add("אורך היום", r.day_hours != null && `${r.day_hours} שעות`);
  add("מקומות", r.want_places.length > 0 && r.want_places.map((p) => PLACE_LABEL[p] ?? p).join(", "));
  add("כשרות", r.kosher_only && "כשר בלבד");
  add("אורך", r.max_length_km != null && `עד ${r.max_length_km} ק״מ`);
  add("נסיעה", r.max_drive_minutes != null && `עד ${r.max_drive_minutes} דק׳`);
  add("יוצאים מ", r.origin_label);
  const g = r.group;
  add("קבוצה", `${g.adults} מבוגרים${g.children ? ` · ${g.children} ילדים` : ""}${g.min_child_age != null ? ` (מגיל ${g.min_child_age})` : ""}`);
  add("כושר", word(g.fitness));
  add("חובה", r.must_have.length > 0 && r.must_have.join(", "));
  add("להימנע", r.avoid.length > 0 && r.avoid.join(", "));
  return chips;
}

/** "HH:MM:SS" (or an ISO datetime) -> "HH:MM". */
export function hhmm(t: string | null | undefined): string {
  if (!t) return "?";
  return (t.split("T").at(-1) ?? t).slice(0, 5);
}

/** 270 -> "4:30". */
export const hoursMinutes = (minutes: number): string =>
  `${Math.floor(minutes / 60)}:${String(Math.round(minutes % 60)).padStart(2, "0")}`;

/** Turns a stream event into one line of the raw event log (or nothing). */
export function describeEvent(ev: StreamEvent): TraceItem | null {
  const item = (tag: string, text: string, tone: TraceItem["tone"] = "normal"): TraceItem => ({ tag, text, tone });
  switch (ev.type) {
    case "node": {
      const round = ev.data.round && ev.data.round > 1 ? ` · סבב ${ev.data.round}` : "";
      return item(ev.data.node, `התחיל: ${AGENT_INFO[ev.data.node].title}${round}`);
    }
    case "request":
      return item("request", `שאילתה סמנטית: ${ev.data.request.semantic_query}`);
    case "tool_calls":
      return item("tools", `במקביל: ${ev.data.calls.map((c) => c.tool).join(" + ")}`);
    case "candidates":
      return item("candidates", `${ev.data.trails.length} מועמדים · ${ev.data.chunks} קטעי ידע`);
    case "assessment": {
      const a = ev.data.assessment;
      const name = a.trail_name ?? a.trail_id;
      if (!a.approved) return item("veto", `${name} נפסל`, "veto");
      return item("safe", `${name} אושר`);
    }
    case "plan":
      return item("plan", ev.data.plan.title);
    case "timing":
      return item("time", `${AGENT_INFO[ev.data.node].title}: ${ev.data.seconds.toFixed(1)}s`);
    case "done":
      return item("done", `${ev.data.tool_calls.length} קריאות כלים`);
    default:
      return null;
  }
}
