// Types mirroring the Python side. Keep in sync with:
//   src/terramind/schemas/plan.py, schemas/safety.py   (domain models)
//   src/terramind/api/sse.py, graph/nodes/*.py emit()  (stream events, one per agent step)
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
  drive_km: number | null;
  drive_method: "geodesic_factor" | "routing_engine" | null;
  drive_route: LatLon[];
  origin: LatLon | null;
  origin_label: string | null;
  depart_at: string | null;
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

export interface TradeOff {
  rejected_trail_id: string;
  rejected_trail_name: string | null;
  factor: string;
  detail: string;
}

export interface FinalAnswer {
  summary_md: string;
  plan: RoutePlan | null; // null => no safe option, the answer explains why
  tradeoffs: TradeOff[];
  safety_notes: string[];
  tips: string[];
}

/** Agent 3's verdict on one candidate trail (safety/rules.py). */
export interface CandidateAssessment {
  trail_id: string;
  trail_name: string | null;
  approved: boolean;
  start_time: string;
  issues: SafetyIssue[];
  weather: WeatherSnapshot | null;
  trail_status: TrailStatus | null;
  alerts: TrailAlert[];
}

export interface TrailAlert {
  alert_id: string;
  trail_id: string | null;
  region: string | null;
  kind: "closure" | "flood" | "fire" | "crowding" | "military" | "maintenance";
  severity: Severity;
  message: string;
}

export interface ToolCall {
  server: string;
  tool: string;
  status: string;
  ms: number;
}

/** Agent 1's output: the free text turned into search parameters (schemas/trip.py). */
export interface TripRequest {
  region: string | null;
  trip_date: string | null;
  start_time: string | null;
  max_duration_hours: number | null;
  difficulty: string | null;
  trail_types: string[];
  max_length_km: number | null;
  max_drive_minutes: number | null;
  origin: LatLon | null;
  origin_label: string | null;
  group: { adults: number; children: number; min_child_age: number | null; fitness: string; pace_kmh: number };
  must_have: string[];
  avoid: string[];
  semantic_query: string;
  missing_fields: string[];
}

export type AgentName = "request_analyst" | "catalog_search" | "enrichment" | "route_planner" | "presenter";

/** Every SSE event the server can send: a discriminated union on `type`. */
export type StreamEvent =
  | { type: "start"; data: { run_id: string } }
  | { type: "node"; data: { node: AgentName; status: "start"; round?: number; relaxed?: boolean; candidates?: number } }
  | { type: "request"; data: { request: TripRequest } }
  | { type: "tool_calls"; data: { calls: { tool: string; args: Record<string, unknown> }[] } }
  | { type: "candidates"; data: { trails: { trail_id: string; name: string; km: number }[]; chunks: number } }
  | { type: "assessment"; data: { assessment: CandidateAssessment } }
  | { type: "plan"; data: { plan: RoutePlan; rationale: string } }
  | { type: "token"; data: { text: string } }
  | { type: "final"; data: { final: FinalAnswer } }
  | { type: "message"; data: { text: string } }
  | { type: "timing"; data: { node: AgentName; seconds: number } }
  | { type: "error"; data: { detail: string } }
  | { type: "done"; data: { tool_calls: ToolCall[] } };

export interface Health {
  status: string;
  mcp_servers: string[];
  llm: string;
}

export interface TrailFeature {
  type: "Feature";
  geometry: GeoJSON.Geometry;
  properties: { trail_id: string; name: string; name_he: string | null };
}

/** GET /pipeline: the last run of each data pipeline job (ingestion_runs). */
export interface PipelineRun {
  last_status: "ok" | "partial" | "failed" | "running";
  last_run_at: string;
  last_good_at: string | null;
  rows: number;
  detail: string | null;
}
export type PipelineStatus = Partial<Record<"weather" | "alerts", PipelineRun>>;

/** "Starting from": the browser's location or a geocoded address. */
export interface Origin {
  lat: number;
  lon: number;
  label: string;
}

export interface Place {
  label: string;
  lat: number;
  lon: number;
}

export interface GeocodeResult {
  status: "ok" | "empty" | "error" | "unavailable";
  detail?: string | null;
  places: Place[];
}
