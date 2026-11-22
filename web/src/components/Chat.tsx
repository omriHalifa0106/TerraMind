import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import type { Turn } from "../hooks/useChat";
import { AGENT_INFO, AGENT_ORDER, hhmm, hoursMinutes } from "../labels";
import { AgentRun } from "./AgentRun";
import { AgentIcon, Alert, Logo, MapPin, Send } from "./icons";
import { Markdown } from "./Markdown";

const EXAMPLES = [
  "מסלול מים קל בגליל העליון לשבת, עם שני ילדים",
  "מסלול מאתגר במירון עם תצפית, יוצאים מוקדם",
  "יום טיול בגולן עם מפלים, רמה בינונית",
];

interface Props {
  turns: Turn[];
  busy: boolean;
  mapTurnId: number | null;
  onSend: (text: string) => void;
  onStop: () => void;
  onShowOnMap: (turnId: number) => void;
}

export function Chat({ turns, busy, mapTurnId, onSend, onStop, onShowOnMap }: Props) {
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true); // follow new output only while the user is at the bottom

  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [turns]);

  return (
    <section className="chat">
      <div
        ref={scroller}
        className="thread"
        aria-live="polite"
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}
      >
        <Welcome onSend={onSend} disabled={busy} compact={turns.length > 0} />
        {turns.map((t) => (
          <TurnView key={t.id} turn={t} onMap={mapTurnId === t.id} onShowOnMap={() => onShowOnMap(t.id)} />
        ))}
      </div>
      <Composer busy={busy} onSend={onSend} onStop={onStop} />
    </section>
  );
}

function BotAvatar() {
  return <Logo className="bot-avatar" />;
}

/** The opening message: what TerraMind is, who the five agents are, and a few examples. */
function Welcome({ onSend, disabled, compact }: { onSend: (t: string) => void; disabled: boolean; compact: boolean }) {
  return (
    <div className="msg bot">
      <BotAvatar />
      <div className="bot-body">
        <div className="bubble">
          <p>
            שלום! ספרו לי איפה, מתי ועם מי תרצו לטייל. <b>חמישה סוכנים</b> יעבדו על הבקשה אחד אחרי השני, ותוכלו לראות
            כאן בדיוק מה כל אחד מהם עושה.
          </p>
          {!compact && (
            <ol className="crew">
              {AGENT_ORDER.map((n, i) => (
                <li key={n}>
                  <span className="mini-avatar">
                    <AgentIcon name={n} className="agent-icon" />
                  </span>
                  <div>
                    <strong>
                      {i + 1}. {AGENT_INFO[n].persona} · {AGENT_INFO[n].title}
                    </strong>
                    <span>{AGENT_INFO[n].role}</span>
                  </div>
                </li>
              ))}
            </ol>
          )}
        </div>
        {!compact && (
          <div className="examples">
            {EXAMPLES.map((t) => (
              <button key={t} type="button" disabled={disabled} onClick={() => onSend(t)}>
                {t}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function TurnView({ turn, onMap, onShowOnMap }: { turn: Turn; onMap: boolean; onShowOnMap: () => void }) {
  const writing = turn.agents.presenter.status === "run";
  return (
    <>
      <div className="msg user">
        <div className="bubble">{turn.query}</div>
      </div>
      <div className="msg bot">
        <BotAvatar />
        <div className="bot-body">
          <AgentRun turn={turn} />
          {turn.error && (
            <div className="bubble error">
              <Alert className="icon" />
              {turn.error}
            </div>
          )}
          {turn.plan && turn.final && <PlanCard turn={turn} onMap={onMap} onShowOnMap={onShowOnMap} />}
          {turn.answer && (
            <div className="bubble answer">
              <Markdown text={turn.answer} streaming={writing} />
            </div>
          )}
        </div>
      </div>
    </>
  );
}

function PlanCard({ turn, onMap, onShowOnMap }: { turn: Turn; onMap: boolean; onShowOnMap: () => void }) {
  const p = turn.plan;
  if (!p) return null;
  return (
    <div className="plan-card">
      <div>
        <span className="eyebrow">המסלול שנבחר</span>
        <strong>{p.title}</strong>
        <span className="stats">
          {p.walk_km.toFixed(1)} ק״מ · {hoursMinutes(p.walk_minutes)} שעות הליכה
          {p.drive_minutes != null ? ` · ${hoursMinutes(p.drive_minutes)} נסיעה` : ""} · {hhmm(p.start_time)}–
          {hhmm(p.end_time)}
        </span>
      </div>
      <button type="button" onClick={onShowOnMap} disabled={onMap}>
        <MapPin className="icon" />
        {onMap ? "מוצג במפה" : "הצג במפה"}
      </button>
    </div>
  );
}

function Composer({ busy, onSend, onStop }: { busy: boolean; onSend: (t: string) => void; onStop: () => void }) {
  const [text, setText] = useState("");
  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const t = text.trim();
    if (!t || busy) return;
    setText("");
    onSend(t);
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };
  return (
    <form className="composer" onSubmit={submit}>
      <label htmlFor="composer" className="sr-only">
        הבקשה שלכם
      </label>
      <textarea
        id="composer"
        rows={1}
        value={text}
        maxLength={2000}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKey}
        placeholder="למשל: מסלול מים קל בגליל העליון לשבת, עם שני ילדים בני 6"
      />
      {busy ? (
        <button type="button" className="stop" onClick={onStop}>
          עצירה
        </button>
      ) : (
        <button type="submit" disabled={!text.trim()} aria-label="שליחה">
          <Send className="icon flip" />
        </button>
      )}
    </form>
  );
}
