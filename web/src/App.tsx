import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api/client";
import type { Health } from "./api/types";
import { Chat } from "./components/Chat";
import { Logo, Wordmark } from "./components/icons";
import { TrailMap } from "./components/TrailMap";
import { useChat, vetoedTrails } from "./hooks/useChat";

function useHealth(): Health | "offline" | null {
  const [health, setHealth] = useState<Health | "offline" | null>(null);
  useEffect(() => {
    api.health().then(setHealth, () => setHealth("offline"));
  }, []);
  return health;
}

/** Demo control for the Mock Alerts API. Goes away once real safety data is connected. */
function useScenarios() {
  const [options, setOptions] = useState<Record<string, string>>({});
  const [current, setCurrent] = useState("");
  useEffect(() => {
    api.scenarios().then(
      (r) => {
        setOptions(r.scenarios);
        setCurrent(r.current);
      },
      () => setOptions({}),
    );
  }, []);
  // The mock API forgets its scenario on restart, so re-send the selection before each message.
  const sync = useCallback(async () => {
    if (current) await api.setScenario(current).catch(() => undefined);
  }, [current]);
  return { options, current, setCurrent, sync };
}

export function App() {
  const health = useHealth();
  const scenarios = useScenarios();
  const { state, send, stop, showOnMap } = useChat(scenarios.sync);

  // The map follows the newest turn, unless the user picked an older plan.
  const mapTurn = useMemo(() => {
    if (state.shownOnMap != null) return state.turns.find((t) => t.id === state.shownOnMap) ?? null;
    return state.turns.at(-1) ?? null;
  }, [state.turns, state.shownOnMap]);
  const vetoed = useMemo(() => vetoedTrails(mapTurn?.candidates ?? []), [mapTurn]);
  const plan = mapTurn?.plan ?? null;
  const chosen = plan ? (mapTurn?.candidates.find((c) => c.trailId === plan.trail_id)?.assessment ?? null) : null;

  const healthOk = health !== null && health !== "offline" && health.mcp_servers.length === 4;
  const healthText =
    health === null ? "…" : health === "offline" ? "השרת לא זמין" : `MCP ${health.mcp_servers.length}/4 · ${health.llm}`;

  return (
    <>
      <header className="topbar">
        <div className="brand">
          <Logo className="logo" />
          <Wordmark />
          <span className="tagline">מתכנן מסלולים עם 5 סוכנים · גליל, מירון, גולן</span>
        </div>
        <div className="controls">
          {Object.keys(scenarios.options).length > 0 && (
            <label className="scenario">
              <span>תרחיש בדיקה</span>
              <select value={scenarios.current} onChange={(e) => scenarios.setCurrent(e.target.value)}>
                {Object.entries(scenarios.options).map(([name, desc]) => (
                  <option key={name} value={name} title={desc}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
          )}
          <span className={`health${healthOk ? "" : " bad"}`} dir="ltr">
            {healthText}
          </span>
        </div>
      </header>

      <main className="layout">
        <Chat
          turns={state.turns}
          busy={state.busy}
          mapTurnId={plan ? (mapTurn?.id ?? null) : null}
          onSend={(t) => void send(t)}
          onStop={stop}
          onShowOnMap={showOnMap}
        />
        <TrailMap plan={plan} vetoed={vetoed} chosen={chosen} />
      </main>
    </>
  );
}
