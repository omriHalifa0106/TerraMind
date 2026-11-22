import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { PipelineRun, PipelineStatus } from "../api/types";

const REFRESH_MS = 60_000;
const LABEL = { weather: "מזג אוויר", alerts: "התראות" } as const;

/** "לפני 3 דק׳" / "16:05" for a timestamp. */
function ago(iso: string, now: number): string {
  const min = Math.round((now - new Date(iso).getTime()) / 60_000);
  if (min < 1) return "עכשיו";
  if (min < 60) return `לפני ${min} דק׳`;
  return new Date(iso).toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit" });
}

function tone(run: PipelineRun | undefined): "ok" | "warn" | "bad" {
  if (!run || !run.last_good_at) return "bad";
  return run.last_status === "ok" ? "ok" : "warn";
}

/**
 * When the data pipeline last stored weather and alerts (the agents read that stored data).
 * Re-fetched every minute and whenever `refreshKey` changes (a run finished, a scenario switch).
 */
export function DataFreshness({ refreshKey }: { refreshKey: unknown }) {
  const [status, setStatus] = useState<PipelineStatus | "offline" | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    let alive = true;
    const load = () => {
      api.pipeline().then(
        (s) => {
          if (alive) {
            setStatus(s);
            setNow(Date.now());
          }
        },
        () => {
          if (alive) setStatus("offline");
        },
      );
    };
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [refreshKey]);

  if (status === null) return null;
  if (status === "offline") return <span className="freshness bad">צינור הנתונים: אין מידע</span>;
  return (
    <span className="freshness" title="מתי צינור הנתונים שמר לאחרונה תחזיות והתראות במסד הנתונים">
      {(["weather", "alerts"] as const).map((k) => {
        const run = status[k];
        const when = run?.last_good_at ? ago(run.last_good_at, now) : "עוד לא נאסף";
        const extra = run && run.last_status !== "ok" ? ` (ריצה אחרונה: ${run.last_status})` : "";
        return (
          <span key={k} className={`fresh-item ${tone(run)}`} title={run?.detail ?? undefined}>
            <i aria-hidden />
            {LABEL[k]}: {when}
            {extra}
          </span>
        );
      })}
    </span>
  );
}
