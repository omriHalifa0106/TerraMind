import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { streamChat } from "../api/stream";
import type {
  AgentName,
  CandidateAssessment,
  FinalAnswer,
  Origin,
  RoutePlan,
  StreamEvent,
  ToolCall,
  TripRequest,
} from "../api/types";
import { AGENT_ORDER, describeEvent, type TraceItem } from "../labels";

export type AgentStatus = "wait" | "run" | "done" | "skip";

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

/** One user message and everything the five agents did to answer it. */
export interface Turn {
  id: number;
  query: string;
  status: "running" | "done" | "stopped";
  followUp: boolean; // the server remembered this chat's previous turn
  scope: { scope: "search" | "replan"; reason: string } | null; // what agent 1 decided to re-run
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
  | { kind: "show"; turnId: number }
  | { kind: "reset" };

const freshAgents = (): Record<AgentName, AgentState> =>
  Object.fromEntries(AGENT_ORDER.map((n) => [n, { status: "wait", startedAt: null, seconds: 0, round: 1 }])) as Record<
    AgentName,
    AgentState
  >;

const newTurn = (id: number, query: string): Turn => ({
  id,
  query,
  status: "running",
  followUp: false,
  scope: null,
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
    case "reset":
      return { turns: [], busy: false, shownOnMap: null };
    case "failed":
      return onLast(state, (t) => ({ ...t, error: action.detail }));
    case "finished":
      return onLast({ ...state, busy: false }, (t) => {
        // A stopped run leaves its running agent unfinished; a completed run closes it, and the
        // agents that never started were not needed (e.g. a follow-up that only re-plans the day).
        const status: AgentStatus = action.aborted ? "wait" : "done";
        const agents = { ...t.agents };
        for (const n of AGENT_ORDER) {
          if (agents[n].status === "run") agents[n] = { ...agents[n], status };
          else if (agents[n].status === "wait" && !action.aborted) agents[n] = { ...agents[n], status: "skip" };
        }
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
    case "start":
      return { ...t, followUp: Boolean(ev.data.follow_up) };
    case "scope":
      return { ...t, scope: ev.data };
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

const THREAD_KEY = "terramind.thread";
const TURNS_KEY = "terramind.turns";
const KEEP_TURNS = 12;

/** One id per chat: the server remembers the chat under it (follow-ups edit the same trip). */
function newThreadId(): string {
  return crypto.randomUUID().replaceAll("-", "");
}

/** The chat as last shown, so a refresh does not lose it (the server holds the real memory). */
function restore(): { threadId: string; turns: Turn[] } {
  try {
    const threadId = localStorage.getItem(THREAD_KEY);
    const turns = JSON.parse(localStorage.getItem(TURNS_KEY) ?? "[]") as Turn[];
    if (threadId && Array.isArray(turns)) {
      return { threadId, turns: turns.map((t) => (t.status === "running" ? { ...t, status: "stopped" } : t)) };
    }
  } catch {
    // storage blocked or corrupted: start a fresh chat
  }
  return { threadId: newThreadId(), turns: [] };
}

function persist(threadId: string, turns: Turn[]): void {
  try {
    localStorage.setItem(THREAD_KEY, threadId);
    localStorage.setItem(TURNS_KEY, JSON.stringify(turns.slice(-KEEP_TURNS)));
  } catch {
    // storage full or blocked: the chat is still remembered by the server, just not redrawn on refresh
  }
}

export function useChat(beforeSend?: () => Promise<void>) {
  const [saved] = useState(restore);
  const [threadId, setThreadId] = useState(saved.threadId);
  const [state, dispatch] = useReducer(reducer, { turns: saved.turns, busy: false, shownOnMap: null });
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!state.busy) persist(threadId, state.turns);
  }, [threadId, state.turns, state.busy]);

  const send = useCallback(
    async (text: string, origin: Origin | null = null, dayHours: number | null = null) => {
      abort.current?.abort();
      const ctrl = new AbortController();
      abort.current = ctrl;
      dispatch({ kind: "send", text, at: Date.now() });
      try {
        await beforeSend?.();
        for await (const event of streamChat(text, origin, dayHours, threadId, ctrl.signal)) {
          dispatch({ kind: "event", event, at: Date.now() });
        }
      } catch (err) {
        if ((err as Error).name !== "AbortError") dispatch({ kind: "failed", detail: `שגיאה: ${(err as Error).message}` });
      } finally {
        // A newer send already replaced this run: its state is no longer ours to close.
        if (abort.current === ctrl) dispatch({ kind: "finished", aborted: ctrl.signal.aborted });
      }
    },
    [beforeSend, threadId],
  );

  /** Closing the stream makes the server stop the run (it checks for disconnects). */
  const stop = useCallback(() => abort.current?.abort(), []);
  const showOnMap = useCallback((turnId: number) => dispatch({ kind: "show", turnId }), []);

  /** A new chat: a new id, so the next message is a new trip (the old chat stays on the server). */
  const newChat = useCallback(() => {
    abort.current?.abort();
    abort.current = null;
    setThreadId(newThreadId());
    dispatch({ kind: "reset" });
  }, []);

  return { state, send, stop, showOnMap, newChat };
}
