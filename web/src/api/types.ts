// Types mirroring the Python side. Keep in sync with:
//   src/terramind/schemas/plan.py, schemas/safety.py   (domain models)
//   src/terramind/api/sse.py, graph/nodes/*.py emit()  (stream events)
// The server serializes times as "HH:MM:SS" and datetimes as ISO strings.

export type LatLon = [number, number];

export type StopKind = "parking" | "trailhead" | "poi" | "viewpoint" | "water" | "food" | "end";

export interface Stop {
  order: number;
  name: string;
  kind: StopKind;
  location: LatLon;
  arrive_at: string | null;
  dwell_minutes: number;
  note: string | null;
}

export interface RoutePlan {
  plan_id: string;
  trail_id: string;
  title: string;
  stops: Stop[];
  walk_km: number;
  walk_minutes: number;
  drive_minutes: number | null;
  start_time: string;
  end_time: string;
  alternatives_considered: string[];
}

export type IssueCode =
  | "CLOSED"
  | "HEAT"
  | "FLOOD_RISK"
  | "STORM"
  | "INSUFFICIENT_DAYLIGHT"
  | "DIFFICULTY_MISMATCH"
  | "CROWDED"
  | "DATA_UNAVAILABLE";

export type Severity = "info" | "warning" | "critical";

export interface SafetyIssue {
  trail_id: string;
  trail_name: string | null;
  code: IssueCode;
  severity: Severity;
  evidence: string;
  source: "rule" | "llm";
}

export interface WeatherSnapshot {
  trail_id: string;
  for_date: string;
  temp_max_c: number | null;
  precip_prob_max_pct: number | null;
  sunset: string | null;
  status: "ok" | "cached" | "out_of_range" | "unavailable";
}

export interface TrailStatus {
  trail_id: string;
  is_open: boolean;
  reason: string | null;
  crowd_level: "low" | "medium" | "high" | "unknown";
  status: "ok" | "unavailable";
}

export interface FinalAnswer {
  summary_md: string;
  plan: RoutePlan | null; // null => no safe option, the answer recommends postponing
  safety_notes: string[];
  tips: string[];
}

export interface ToolCall {
  server: string;
  tool: string;
  status: string;
  ms: number;
}

/** Every SSE event the server can send: a discriminated union on `type`. */
export type StreamEvent =
  | { type: "start"; data: { thread_id: string } }
  | { type: "node"; data: { node: string; status: "start" | "done"; round?: number; relaxed?: boolean } }
  | { type: "node_done"; data: { node: string } }
  | { type: "route"; data: { intent: string } }
  | { type: "request"; data: { request: Record<string, unknown> } }
  | { type: "candidates"; data: { trails: { trail_id: string; name: string; km: number }[] } }
  | { type: "graded"; data: { kept: number; total: number } }
  | { type: "plan"; data: { plan: RoutePlan; rationale: string } }
  | {
      type: "conditions";
      data: { weather: WeatherSnapshot | null; status: TrailStatus | null; alerts: unknown[] };
    }
  | {
      type: "verdict";
      data: { attempt: number; approved: boolean; scope: "none" | "timing" | "trail"; issues: SafetyIssue[] };
    }
  | { type: "token"; data: { text: string } }
  | { type: "final"; data: { final: FinalAnswer } }
  | { type: "message"; data: { text: string } }
  | { type: "warning"; data: { detail: string } }
  | { type: "error"; data: { detail: string } }
  | { type: "done"; data: { tool_calls: ToolCall[] } };

export interface Health {
  status: string;
  mcp_servers: string[];
  llm: string;
  llm_provider: string;
  persistent_memory: boolean;
}

export interface TrailFeature {
  type: "Feature";
  geometry: GeoJSON.Geometry;
  properties: { trail_id: string; name: string; name_he: string | null };
}
