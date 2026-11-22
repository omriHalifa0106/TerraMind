import { useCallback, useReducer, useRef } from "react";
import { streamChat } from "../api/stream";
import type {
  AgentName,
  CandidateAssessment,
  FinalAnswer,
  RoutePlan,
  StreamEvent,
  ToolCall,
  TripRequest,
} from "../api/types";
import { AGENT_ORDER, describeEvent, type TraceItem } from "../labels";

export type AgentStatus = "wait" | "run" | "done";

export interface AgentState {
  status: AgentStatus;
  startedAt: number | null; // ms timestamp of the latest start (live timer)
  seconds: number; // server-measured, summed over rounds
  round: number;
}

/** A trail found by agent 2, plus agent 3's verdict once it arrives. */
export interface Candidate {
  trailId: string;
  name: string;
  km: number | null;
  assessment: CandidateAssessment | null;
}

export interface VetoedTrail {
  trailId: string;
  reasons: string[];
}

/** One user message and everything the five agents did to answer it. Turns are independent. */
export interface Turn {
  id: number;
  query: string;
  status: "running" | "done" | "stopped";
  agents: Record<AgentName, AgentState>;
  request: TripRequest | null;
  searchCalls: string[]; // tools agent 2's LLM chose (they run in parallel)
  chunks: number | null;
  candidates: Candidate[];
  plan: RoutePlan | null;
  rationale: string | null;
  final: FinalAnswer | null;
  answer: string; // agent 5's Markdown (streamed), or a clarification question
  error: string | null;
  log: TraceItem[];
  toolCalls: ToolCall[];
}

export interface ChatState {
  turns: Turn[];
  busy: boolean;
  shownOnMap: number | null; // turn id whose plan the map shows (default: latest with a plan)
}

type Action =
  | { kind: "send"; text: string; at: number }
  | { kind: "event"; event: StreamEvent; at: number }
  | { kind: "failed"; detail: string }
  | { kind: "finished"; aborted: boolean }
  | { kind: "show"; turnId: number };

const freshAgents = (): Record<AgentName, AgentState> =>
  Object.fromEntries(AGENT_ORDER.map((n) => [n, { status: "wait", startedAt: null, seconds: 0, round: 1 }])) as Record<
    AgentName,
    AgentState
  >;

const newTurn = (id: number, query: string): Turn => ({
  id,
  query,
  status: "running",
  agents: freshAgents(),
  request: null,
  searchCalls: [],
  chunks: null,
  candidates: [],
  plan: null,
  rationale: null,
  final: null,
  answer: "",
  error: null,
  log: [],
  toolCalls: [],
});

function setAgent(t: Turn, name: AgentName, patch: Partial<AgentState>): Turn {
  return { ...t, agents: { ...t.agents, [name]: { ...t.agents[name], ...patch } } };
}

/** Applies an update to the newest turn (the only one that can still be running). */
function onLast(state: ChatState, f: (t: Turn) => Turn): ChatState {
  const last = state.turns.at(-1);
  return last ? { ...state, turns: [...state.turns.slice(0, -1), f(last)] } : state;
}

/** All UI state changes in one pure function: easy to follow, easy to test. */
function reducer(state: ChatState, action: Action): ChatState {
  switch (action.kind) {
    case "send": {
      const id = (state.turns.at(-1)?.id ?? 0) + 1;
      return { turns: [...state.turns, newTurn(id, action.text)], busy: true, shownOnMap: null };
    }
    case "show":
      return { ...state, shownOnMap: action.turnId };
    case "failed":
      return onLast(state, (t) => ({ ...t, error: action.detail }));
    case "finished":
      return onLast({ ...state, busy: false }, (t) => {
        // A stopped run leaves its running agent unfinished; a completed run closes it.
        const status: AgentStatus = action.aborted ? "wait" : "done";
        const agents = { ...t.agents };
        for (const n of AGENT_ORDER) if (agents[n].status === "run") agents[n] = { ...agents[n], status };
        return { ...t, agents, status: action.aborted ? "stopped" : "done" };
      });
    case "event":
      return onLast(state, (t) => applyEvent(t, action.event, action.at));
  }
}

function applyEvent(turn: Turn, ev: StreamEvent, at: number): Turn {
  const item = describeEvent(ev);
  const t: Turn = item ? { ...turn, log: [...turn.log, item] } : turn;

  switch (ev.type) {
    case "node": {
      // An agent started. A second search round restarts agent 2, so later agents go back to waiting.
      const started = AGENT_ORDER.indexOf(ev.data.node);
      const agents = { ...t.agents };
      AGENT_ORDER.forEach((n, i) => {
        if (i < started && agents[n].status === "run") agents[n] = { ...agents[n], status: "done" };
        if (i > started && agents[n].status !== "wait") agents[n] = { ...agents[n], status: "wait" };
      });
      agents[ev.data.node] = { ...agents[ev.data.node], status: "run", startedAt: at, round: ev.data.round ?? 1 };
      return { ...t, agents };
    }
    case "timing":
      return setAgent(t, ev.data.node, { status: "done", seconds: t.agents[ev.data.node].seconds + ev.data.seconds });
    case "request":
      return { ...t, request: ev.data.request };
    case "tool_calls":
      return { ...t, searchCalls: ev.data.calls.map((c) => c.tool), chunks: null };
    case "candidates": {
      const known = new Set(t.candidates.map((c) => c.trailId));
      const added = ev.data.trails
        .filter((x) => !known.has(x.trail_id))
        .map((x): Candidate => ({ trailId: x.trail_id, name: x.name, km: x.km, assessment: null }));
      return { ...t, candidates: [...t.candidates, ...added], chunks: ev.data.chunks };
    }
    case "assessment": {
      const a = ev.data.assessment;
      const candidates = t.candidates.some((c) => c.trailId === a.trail_id)
        ? t.candidates.map((c) => (c.trailId === a.trail_id ? { ...c, assessment: a } : c))
        : [...t.candidates, { trailId: a.trail_id, name: a.trail_name ?? a.trail_id, km: null, assessment: a }];
      return { ...t, candidates };
    }
    case "plan":
      return { ...t, plan: ev.data.plan, rationale: ev.data.rationale };
    case "token":
      return { ...t, answer: t.answer + ev.data.text };
    case "message":
      return { ...t, answer: ev.data.text };
    case "final": {
      const f = ev.data.final;
      return { ...t, final: f, plan: f.plan ?? t.plan, answer: t.answer || f.summary_md };
    }
    case "error":
      return { ...t, error: ev.data.detail };
    case "done":
      return { ...t, toolCalls: ev.data.tool_calls };
    default:
      return t;
  }
}

/** Unsafe trails, for the map's dashed red layer. */
export function vetoedTrails(candidates: Candidate[]): VetoedTrail[] {
  return candidates
    .filter((c) => c.assessment && !c.assessment.approved)
    .map((c) => ({
      trailId: c.trailId,
      reasons: (c.assessment?.issues ?? []).filter((i) => i.severity === "critical").map((i) => i.evidence),
    }));
}

export function useChat(beforeSend?: () => Promise<void>) {
  const [state, dispatch] = useReducer(reducer, { turns: [], busy: false, shownOnMap: null });
  const abort = useRef<AbortController | null>(null);

  const send = useCallback(
    async (text: string) => {
      abort.current?.abort();
      const ctrl = new AbortController();
      abort.current = ctrl;
      dispatch({ kind: "send", text, at: Date.now() });
      try {
        await beforeSend?.();
        for await (const event of streamChat(text, ctrl.signal)) {
          dispatch({ kind: "event", event, at: Date.now() });
        }
      } catch (err) {
        if ((err as Error).name !== "AbortError") dispatch({ kind: "failed", detail: `שגיאה: ${(err as Error).message}` });
      } finally {
        // A newer send already replaced this run: its state is no longer ours to close.
        if (abort.current === ctrl) dispatch({ kind: "finished", aborted: ctrl.signal.aborted });
      }
    },
    [beforeSend],
  );

  /** Closing the stream makes the server stop the run (it checks for disconnects). */
  const stop = useCallback(() => abort.current?.abort(), []);
  const showOnMap = useCallback((turnId: number) => dispatch({ kind: "show", turnId }), []);

  return { state, send, stop, showOnMap };
}
