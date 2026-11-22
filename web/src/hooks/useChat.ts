import { useCallback, useReducer, useRef } from "react";
import { streamChat } from "../api/stream";
import type { RoutePlan, StreamEvent } from "../api/types";
import { describeEvent, type TraceItem } from "../labels";

export interface ChatMessage {
  id: number;
  role: "user" | "bot" | "error";
  text: string;
  streaming?: boolean;
}

export interface VetoedTrail {
  trailId: string;
  reasons: string[];
}

export interface ChatState {
  threadId: string | null;
  busy: boolean;
  messages: ChatMessage[];
  trace: TraceItem[];
  plan: RoutePlan | null;
  vetoed: VetoedTrail[];
}

type Action =
  | { kind: "send"; text: string }
  | { kind: "event"; event: StreamEvent }
  | { kind: "failed"; detail: string }
  | { kind: "finished" };

const THREAD_KEY = "tm-thread";
const GREETING = "שלום! ספרו לי איפה, מתי ועם מי תרצו לטייל, ואבנה לכם מסלול עם בדיקת בטיחות והסבר.";

let nextId = 1;
const msg = (role: ChatMessage["role"], text: string, streaming = false): ChatMessage => ({
  id: nextId++,
  role,
  text,
  streaming,
});

function readThread(): string | null {
  try {
    return localStorage.getItem(THREAD_KEY);
  } catch {
    return null; // private mode / blocked storage: start a fresh conversation
  }
}

function saveThread(id: string): void {
  try {
    localStorage.setItem(THREAD_KEY, id);
  } catch {
    /* best effort */
  }
}

const initial = (): ChatState => ({
  threadId: readThread(),
  busy: false,
  messages: [msg("bot", GREETING)],
  trace: [],
  plan: null,
  vetoed: [],
});

/** All UI state changes in one pure function: easy to follow, easy to test. */
function reducer(state: ChatState, action: Action): ChatState {
  switch (action.kind) {
    case "send":
      return { ...state, busy: true, messages: [...state.messages, msg("user", action.text)], trace: [], vetoed: [] };
    case "failed":
      return { ...state, messages: [...state.messages, msg("error", action.detail)] };
    case "finished":
      return { ...state, busy: false, messages: state.messages.map((m) => ({ ...m, streaming: false })) };
    case "event":
      return applyEvent(state, action.event);
  }
}

function applyEvent(state: ChatState, ev: StreamEvent): ChatState {
  const item = describeEvent(ev);
  const s = item ? { ...state, trace: [...state.trace, item] } : state;

  switch (ev.type) {
    case "start":
      return { ...s, threadId: ev.data.thread_id };
    case "token": {
      // Append to the bot message being streamed, or start one.
      const last = s.messages.at(-1);
      if (last?.role === "bot" && last.streaming) {
        return { ...s, messages: [...s.messages.slice(0, -1), { ...last, text: last.text + ev.data.text }] };
      }
      return { ...s, messages: [...s.messages, msg("bot", ev.data.text, true)] };
    }
    case "final":
      return { ...s, plan: ev.data.final.plan ?? s.plan };
    case "message":
      return { ...s, messages: [...s.messages, msg("bot", ev.data.text)] };
    case "error":
      return { ...s, messages: [...s.messages, msg("error", ev.data.detail)] };
    case "verdict": {
      if (ev.data.approved || ev.data.scope !== "trail") return s;
      const critical = ev.data.issues.filter((i) => i.severity === "critical");
      const first = critical[0];
      if (!first) return s;
      return { ...s, vetoed: [...s.vetoed, { trailId: first.trail_id, reasons: critical.map((i) => i.evidence) }] };
    }
    default:
      return s;
  }
}

export function useChat(userId: string, beforeSend?: () => Promise<void>) {
  const [state, dispatch] = useReducer(reducer, undefined, initial);
  const abort = useRef<AbortController | null>(null);
  const threadRef = useRef(state.threadId);
  threadRef.current = state.threadId;

  const send = useCallback(
    async (text: string) => {
      dispatch({ kind: "send", text });
      abort.current = new AbortController();
      try {
        await beforeSend?.();
        for await (const event of streamChat(
          { message: text, thread_id: threadRef.current, user_id: userId },
          abort.current.signal,
        )) {
          // Side effects stay out of the reducer (React may run reducers twice in dev).
          if (event.type === "start") saveThread(event.data.thread_id);
          dispatch({ kind: "event", event });
        }
      } catch (err) {
        if ((err as Error).name !== "AbortError") dispatch({ kind: "failed", detail: `שגיאה: ${(err as Error).message}` });
      } finally {
        dispatch({ kind: "finished" });
      }
    },
    [userId, beforeSend],
  );

  /** Closing the stream makes the server stop the run (it checks for disconnects). */
  const stop = useCallback(() => abort.current?.abort(), []);

  return { state, send, stop };
}
