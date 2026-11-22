import { useCallback, useEffect, useState } from "react";
import { api } from "./api/client";
import type { Health } from "./api/types";
import { Composer, Messages, Trace } from "./components/Chat";
import { TrailMap } from "./components/TrailMap";
import { useChat } from "./hooks/useChat";

// Until there is a login (backend hardening step), every browser is the same demo user.
const USER_ID = "demo-user";

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
  const { state, send, stop } = useChat(USER_ID, scenarios.sync);

  const healthOk = health !== null && health !== "offline" && health.mcp_servers.length === 4;
  const healthText =
    health === null ? "…" : health === "offline" ? "API offline" : `MCP ${health.mcp_servers.length}/4 · ${health.llm}`;

  return (
    <>
      <header className="topbar">
        <div className="brand">
          <img src="/logo.svg" alt="" className="logo-img" />
          <div>
            <h1>TerraMind</h1>
            <p>מתכנן מסלולים אוטונומי · גליל, מירון, גולן</p>
          </div>
        </div>
        <div className="controls">
          {Object.keys(scenarios.options).length > 0 && (
            <>
              <label htmlFor="scenario">תרחיש בדיקה</label>
              <select
                id="scenario"
                value={scenarios.current}
                onChange={(e) => scenarios.setCurrent(e.target.value)}
              >
                {Object.entries(scenarios.options).map(([name, desc]) => (
                  <option key={name} value={name} title={desc}>
                    {name}
                  </option>
                ))}
              </select>
            </>
          )}
          <span className={`health${healthOk ? "" : " bad"}`}>{healthText}</span>
        </div>
      </header>

      <main className="layout">
        <section className="panel">
          <Messages messages={state.messages} />
          <Trace items={state.trace} />
          <Composer busy={state.busy} onSend={(t) => void send(t)} onStop={stop} />
        </section>
        <TrailMap plan={state.plan} vetoed={state.vetoed} />
      </main>
    </>
  );
}
