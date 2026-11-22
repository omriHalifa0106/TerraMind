import { useEffect, useState, type ReactNode } from "react";
import type { AgentName, CandidateAssessment, IssueCode } from "../api/types";
import type { AgentState, Candidate, Turn } from "../hooks/useChat";
import { AGENT_INFO, AGENT_ORDER, PLACE_LABEL, TOOL_LABEL, hhmm, requestChips, toolOwner } from "../labels";
import { AgentIcon, Check, Chevron } from "./icons";

/** Re-renders every 100 ms while `active`, so running agents show a live timer. */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(id);
  }, [active]);
  return now;
}

function elapsed(a: AgentState, now: number): string | null {
  if (a.status === "run" && a.startedAt) return `${(a.seconds + (now - a.startedAt) / 1000).toFixed(1)}s`;
  if (a.seconds > 0) return `${a.seconds.toFixed(1)}s`;
  return null;
}

/**
 * The five agents of one turn, visibly at work: a pipeline of avatars on top, and below it one
 * card per agent showing its input, what it is doing right now, and what it hands to the next one.
 * Open while the run is live; afterwards it folds into a one-line summary.
 */
export function AgentRun({ turn }: { turn: Turn }) {
  const running = turn.status === "running";
  const now = useNow(running);
  const [open, setOpen] = useState<boolean | null>(null); // null = follow the run
  const expanded = open ?? running;
  const active = AGENT_ORDER.find((n) => turn.agents[n].status === "run");
  const total = AGENT_ORDER.reduce((s, n) => s + turn.agents[n].seconds, 0);

  return (
    <section className={`run${running ? " live" : ""}`} aria-label="הסוכנים בעבודה">
      {turn.followUp && turn.scope && (
        <div className={`scope-note ${turn.scope.scope}`}>
          <b>המשך לשיחה</b> · {turn.scope.reason}
          {turn.scope.scope === "replan"
            ? ". החוקר והשומר לא רצו שוב: המסלולים כבר נבדקו בהודעה הקודמת, והמתכנן בונה מחדש את היום."
            : ". כל הסוכנים רצים מחדש."}
        </div>
      )}
      <Pipeline turn={turn} now={now} />

      <button type="button" className="run-toggle" onClick={() => setOpen(!expanded)} aria-expanded={expanded}>
        {active ? (
          <span className="status-line">
            <span className="typing" aria-hidden>
              <i />
              <i />
              <i />
            </span>
            <b>{AGENT_INFO[active].persona}:</b> {AGENT_INFO[active].working}
          </span>
        ) : (
          <span className="status-line">
            {turn.status === "stopped" ? "הריצה נעצרה" : "איך הסוכנים הגיעו לתשובה?"}
            {total > 0 && (
              <span className="mono">
                {" "}
                · {total.toFixed(1)}s{turn.toolCalls.length > 0 && ` · ${turn.toolCalls.length} קריאות כלים`}
              </span>
            )}
          </span>
        )}
        <Chevron className={`icon chev${expanded ? " up" : ""}`} />
      </button>

      {expanded && (
        <ol className="steps">
          {AGENT_ORDER.filter((n) => turn.agents[n].status !== "wait" || hasOutput(turn, n)).map((n) => (
            <Step key={n} name={n} turn={turn} now={now} />
          ))}
        </ol>
      )}
    </section>
  );
}

function hasOutput(t: Turn, n: AgentName): boolean {
  if (n === "request_analyst") return t.request != null;
  if (n === "catalog_search") return t.candidates.length > 0;
  if (n === "enrichment") return t.candidates.some((c) => c.assessment);
  if (n === "route_planner") return t.plan != null;
  return false;
}

/** Five avatars joined by tracks; data "flows" along the track into the agent that is working. */
function Pipeline({ turn, now }: { turn: Turn; now: number }) {
  return (
    <div className="pipeline">
      {AGENT_ORDER.map((n, i) => {
        const a = turn.agents[n];
        const next = AGENT_ORDER[i + 1];
        const track = next
          ? turn.agents[next].status === "run"
            ? "flow"
            : turn.agents[next].status === "done"
              ? "done"
              : ""
          : null;
        return (
          <div key={n} className="pipe-cell">
            <div className={`agent ${a.status}`}>
              <span className="avatar">
                <AgentIcon name={n} className="agent-icon" />
                <span className="num">{a.status === "done" ? <Check className="tick" /> : i + 1}</span>
              </span>
              <span className="persona">{AGENT_INFO[n].persona}</span>
              <span className="clock">{elapsed(a, now) ?? " "}</span>
            </div>
            {track !== null && (
              <span className={`track ${track}`} aria-hidden>
                <i />
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

function Step({ name, turn, now }: { name: AgentName; turn: Turn; now: number }) {
  const a = turn.agents[name];
  const info = AGENT_INFO[name];
  const idx = AGENT_ORDER.indexOf(name);
  const used = countTools(turn, name);
  const handoff = handoffText(turn, name);
  const nextAgent = AGENT_ORDER[idx + 1];
  return (
    <li className={`step ${a.status}`}>
      <div className="step-head">
        <span className="mini-avatar">
          <AgentIcon name={name} className="agent-icon" />
        </span>
        <div className="step-title">
          <strong>
            סוכן {idx + 1} · {info.title}
            {a.round > 1 && <span className="round">סבב {a.round}: חיפוש רחב יותר</span>}
          </strong>
          <span className="engine">{info.engine}</span>
        </div>
        <span className={`state ${a.status}`}>
          {a.status === "run" ? "עובד" : a.status === "done" ? "סיים" : a.status === "skip" ? "דולג" : "ממתין"}
          {elapsed(a, now) && <span className="mono"> {elapsed(a, now)}</span>}
        </span>
      </div>
      <p className="role">{info.role}</p>
      <div className="step-body">{body(name, turn)}</div>
      {used.length > 0 && (
        <div className="tools-used" dir="ltr">
          {used.map(([tool, n, ms]) => (
            <span key={tool}>
              {tool}
              {n > 1 ? ` ×${n}` : ""} <em>{ms}ms</em>
            </span>
          ))}
        </div>
      )}
      {handoff && a.status === "done" && (
        <div className="handoff">
          <span className="arrow" aria-hidden>
            ↓
          </span>
          {nextAgent ? `מעביר ל${AGENT_INFO[nextAgent].persona}: ` : ""}
          {handoff}
        </div>
      )}
    </li>
  );
}

/** [tool, calls, slowest ms] for the MCP tools this agent called (known after the run). */
function countTools(turn: Turn, name: AgentName): [string, number, number][] {
  const m = new Map<string, [number, number]>();
  for (const c of turn.toolCalls) {
    if (toolOwner(c.tool) !== name) continue;
    const [n, ms] = m.get(c.tool) ?? [0, 0];
    m.set(c.tool, [n + 1, Math.max(ms, c.ms)]);
  }
  return [...m.entries()].map(([tool, [n, ms]]) => [tool, n, ms]);
}

function handoffText(t: Turn, name: AgentName): string | null {
  switch (name) {
    case "request_analyst":
      return t.request ? `${requestChips(t.request).length} פרמטרים + שאילתה סמנטית` : null;
    case "catalog_search":
      return `${t.candidates.length} מסלולים מועמדים + ${t.chunks ?? 0} קטעי ידע`;
    case "enrichment": {
      const ok = t.candidates.filter((c) => c.assessment?.approved).length;
      const bad = t.candidates.filter((c) => c.assessment && !c.assessment.approved).length;
      return `${ok} מסלולים בטוחים בלבד${bad ? ` (${bad} נפסלו ולא ימשיכו)` : ""}`;
    }
    case "route_planner":
      return t.plan ? `תוכנית של ${t.plan.stops.length} תחנות, ${hhmm(t.plan.start_time)}–${hhmm(t.plan.end_time)}` : null;
    case "presenter":
      return t.answer ? "התשובה כתובה למטה" : null;
  }
}

const Skeleton = ({ lines = 2 }: { lines?: number }) => (
  <div className="skeleton" aria-hidden>
    {Array.from({ length: lines }, (_, i) => (
      <i key={i} />
    ))}
  </div>
);

function body(name: AgentName, t: Turn): ReactNode {
  switch (name) {
    case "request_analyst":
      if (!t.request) return <Skeleton />;
      return (
        <>
          <div className="chips">
            {requestChips(t.request).map((c) => (
              <span key={c.label} className="chip">
                <em>{c.label}</em>
                {c.value}
              </span>
            ))}
          </div>
          <div className="quote">
            <em>שאילתה לחיפוש סמנטי</em>„{t.request.semantic_query}”
          </div>
        </>
      );
    case "catalog_search":
      return <SearchLanes t={t} />;
    case "enrichment":
      return (
        <SafetyBoard
          candidates={t.candidates}
          running={t.agents.enrichment.status === "run"}
          finished={t.agents.enrichment.status === "done" || t.status !== "running"}
        />
      );
    case "route_planner":
      if (!t.plan) return <Skeleton lines={3} />;
      return (
        <>
          <ol className="mini-stops">
            {t.plan.stops.map((s, i) => (
              <li key={i} className={s.place_id ? "place" : undefined}>
                <span className="mono">{hhmm(s.arrive_at)}</span>
                <span>
                  {s.name}
                  {s.category && <em className="kind-tag">{PLACE_LABEL[s.category] ?? s.category}</em>}
                  {s.kosher && <em className="kind-tag">כשר</em>}
                  {s.place_id && (
                    <span className="place-meta">
                      {s.drive_minutes != null && `נסיעה ${s.drive_minutes} דק׳ · `}
                      {s.dwell_minutes} דק׳ במקום ·{" "}
                      {s.open_state === "open" ? "פתוח" : s.open_state === "unknown" ? "שעות פתיחה לא ידועות" : ""}
                    </span>
                  )}
                  {s.place_id && s.note && <span className="place-meta">{s.note}</span>}
                </span>
              </li>
            ))}
          </ol>
          {t.plan.start_note && <p className="skipped">{t.plan.start_note}</p>}
          {(t.plan.dropped_places ?? []).length > 0 && (
            <p className="skipped">לא נכנסו לתוכנית: {(t.plan.dropped_places ?? []).join(" · ")}</p>
          )}
          <DriveLine t={t} />
          {t.rationale && (
            <div className="quote">
              <em>למה המסלול הזה</em>
              {t.rationale}
            </div>
          )}
        </>
      );
    case "presenter":
      return t.answer ? (
        <div className="writing">
          {t.agents.presenter.status === "run" ? "כותב" : "נכתבו"} <span className="mono">{t.answer.length}</span> תווים
          {t.agents.presenter.status === "run" && <span className="pen" aria-hidden />}
        </div>
      ) : (
        <Skeleton lines={1} />
      );
  }
}

/** Agent 4's drive leg: real roads from OSRM, or the straight-line fallback (said so). */
function DriveLine({ t }: { t: Turn }) {
  const p = t.plan;
  if (!p) return null;
  if (p.drive_minutes == null) {
    return <div className="drive-line muted">לא נבחרה נקודת יציאה, לכן אין זמן נסיעה.</div>;
  }
  const engine = p.drive_method === "routing_engine" ? "OSRM · כבישים אמיתיים" : "הערכה בקו אווירי (OSRM לא זמין)";
  return (
    <div className="drive-line">
      <span>
        נסיעה מ{p.origin_label ?? "נקודת היציאה"}: <b>{p.drive_minutes} דק׳</b>
        {p.drive_km != null && ` · ${p.drive_km.toFixed(0)} ק״מ`} · יציאה ב-<b>{hhmm(p.depart_at)}</b>
      </span>
      <span className="engine-tag">{engine}</span>
    </div>
  );
}

/** Agent 2: the tools its LLM chose, running side by side, then the trails they found. */
function SearchLanes({ t }: { t: Turn }) {
  const working = t.agents.catalog_search.status === "run";
  const finished = t.chunks != null;
  return (
    <>
      {t.searchCalls.length === 0 ? (
        working ? (
          <div className="thinking">Claude בוחר אילו כלים להפעיל…</div>
        ) : null
      ) : (
        <div className="lanes">
          {t.searchCalls.map((tool) => (
            <div key={tool} className={`lane${finished ? " done" : ""}`}>
              <span className="lane-name">{TOOL_LABEL[tool] ?? tool}</span>
              <span className="lane-tool mono">{tool}()</span>
              <span className="lane-result">
                {!finished ? (
                  <span className="spinner" aria-label="רץ" />
                ) : tool === "hybrid_search" ? (
                  `${t.chunks} קטעי ידע`
                ) : (
                  `${t.candidates.length} מסלולים`
                )}
              </span>
            </div>
          ))}
          <span className="parallel" aria-hidden>
            במקביל
          </span>
        </div>
      )}
      {t.candidates.length > 0 && (
        <div className="chips">
          {t.candidates.map((c, i) => (
            <span key={c.trailId} className="chip trail" style={{ animationDelay: `${i * 60}ms` }}>
              {c.name}
              {c.km ? <em>{c.km.toFixed(1)} ק״מ</em> : null}
            </span>
          ))}
        </div>
      )}
    </>
  );
}

type Tone = "ok" | "warn" | "bad" | "pending";

/** Three checks per trail, each colored only by the issues that belong to it. */
function checks(a: CandidateAssessment): { label: string; text: string; tone: Tone }[] {
  const worst = (codes: IssueCode[]): Tone | null => {
    const hit = a.issues.filter((i) => codes.includes(i.code) && i.severity !== "info");
    if (hit.some((i) => i.severity === "critical")) return "bad";
    return hit.length ? "warn" : null;
  };
  // FLOOD_RISK comes from a flood alert or from the rain forecast: credit the right check.
  const floodAlert = a.alerts.some((x) => x.kind === "flood");
  const w = a.weather;
  const wOk = w && (w.status === "ok" || w.status === "cached");
  const s = a.trail_status;
  const known = s && s.status === "ok";
  const alertTone: Tone = a.alerts.length === 0 ? "ok" : (worst(["FLOOD_RISK", "CLOSED"]) ?? "warn");
  const list = [
    {
      label: "מזג אוויר",
      text: wOk ? `${w.temp_max_c ?? "?"}° · גשם ${w.precip_prob_max_pct ?? "?"}%` : "אין תחזית",
      tone: worst(floodAlert ? ["HEAT", "STORM"] : ["HEAT", "STORM", "FLOOD_RISK"]) ?? (wOk ? "ok" : "warn"),
    },
    {
      label: "סגירות",
      text: !known ? "לא ידוע" : s.is_open ? (s.crowd_level === "high" ? "פתוח · עמוס" : "פתוח") : "סגור",
      tone: (!known ? "warn" : !s.is_open ? "bad" : s.crowd_level === "high" ? "warn" : "ok") as Tone,
    },
    {
      label: "התראות",
      text: a.alerts.length ? `${a.alerts.length} פעילות` : "אין",
      tone: alertTone,
    },
  ];
  const daylight = worst(["INSUFFICIENT_DAYLIGHT"]);
  if (daylight) list.push({ label: "אור יום", text: "לא מספיק אור יום", tone: daylight });
  return list;
}

/** Agent 3: every candidate gets three checks, then a verdict decided by fixed rules. */
function SafetyBoard({ candidates, running, finished }: { candidates: Candidate[]; running: boolean; finished: boolean }) {
  if (candidates.length === 0) return <Skeleton />;
  const skipped = finished ? candidates.filter((c) => !c.assessment) : [];
  return (
    <>
    <ul className="safety">
      {candidates.filter((c) => !skipped.includes(c)).map((c, i) => {
        const a = c.assessment;
        const verdict = !a ? "pending" : a.approved ? "ok" : "bad";
        const reasons = a?.issues.filter((x) => x.severity === "critical").map((x) => x.evidence) ?? [];
        return (
          <li key={c.trailId} className={verdict} style={{ animationDelay: `${i * 80}ms` }}>
            <span className="trail-name">{c.name}</span>
            <span className="checks">
              {(a ? checks(a) : [{ label: "מזג אוויר" }, { label: "סגירות" }, { label: "התראות" }]).map((k) => {
                const tone: Tone = "tone" in k ? k.tone : "pending";
                return (
                  <span key={k.label} className={`check ${tone}`} title={k.label}>
                    {tone === "pending" ? running ? <span className="spinner small" /> : "·" : null}
                    <span>{"text" in k ? k.text : k.label}</span>
                  </span>
                );
              })}
            </span>
            <span className={`verdict ${verdict}`}>{!a ? "בבדיקה" : a.approved ? "בטוח" : "נפסל"}</span>
            {reasons.length > 0 && <span className="reasons">{reasons.join(" · ")}</span>}
          </li>
        );
      })}
    </ul>
    {skipped.length > 0 && (
      <p className="skipped">
        לא נבדקו ({skipped.length}): {skipped.map((c) => c.name).join(", ")}. השומר בודק רק את{" "}
        המועמדים המובילים בכל סבב.
      </p>
    )}
    </>
  );
}
