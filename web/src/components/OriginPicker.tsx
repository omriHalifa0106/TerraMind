import { useEffect, useId, useRef, useState } from "react";
import { api } from "../api/client";
import type { Origin, Place } from "../api/types";
import { MapPin } from "./icons";

const STORAGE_KEY = "terramind.origin";
const MIN_QUERY = 2;
const DEBOUNCE_MS = 450; // the geocoder allows ~1 request/s, so wait until the user pauses typing

/** The last chosen starting point, remembered in this browser (a convenience only). */
export function loadOrigin(): Origin | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const o = raw ? (JSON.parse(raw) as Origin) : null;
    return o && typeof o.lat === "number" && typeof o.lon === "number" && o.label ? o : null;
  } catch {
    return null;
  }
}

export function saveOrigin(o: Origin | null): void {
  try {
    if (o) localStorage.setItem(STORAGE_KEY, JSON.stringify(o));
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    // storage blocked (private window): the choice just isn't remembered
  }
}

type Mode = "idle" | "address" | "locating";

/**
 * "יוצאים מ": where the group drives from. Either the browser's location, or an address looked up
 * through the gis MCP server (OpenStreetMap). Agent 4 uses it for real road drive times.
 */
export function OriginPicker({ origin, onChange }: { origin: Origin | null; onChange: (o: Origin | null) => void }) {
  const [mode, setMode] = useState<Mode>("idle");
  const [error, setError] = useState<string | null>(null);

  const locate = () => {
    if (!("geolocation" in navigator)) {
      setError("הדפדפן לא תומך במיקום");
      return;
    }
    setMode("locating");
    setError(null);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        onChange({ lat: pos.coords.latitude, lon: pos.coords.longitude, label: "המיקום שלי" });
        setMode("idle");
      },
      (err) => {
        setError(err.code === err.PERMISSION_DENIED ? "אין הרשאת מיקום. אפשר להקליד כתובת." : "לא הצלחתי לאתר מיקום");
        setMode("idle");
      },
      { enableHighAccuracy: false, timeout: 10_000, maximumAge: 5 * 60_000 },
    );
  };

  return (
    <div className="origin">
      <span className="origin-label">
        <MapPin className="icon" />
        יוצאים מ:
      </span>
      {mode === "address" ? (
        <AddressSearch
          onPick={(p) => {
            onChange({ lat: p.lat, lon: p.lon, label: p.label });
            setMode("idle");
          }}
          onCancel={() => setMode("idle")}
        />
      ) : (
        <>
          {origin ? (
            <span className="origin-chip">
              {origin.label}
              <button type="button" aria-label="ביטול נקודת היציאה" onClick={() => onChange(null)}>
                ×
              </button>
            </span>
          ) : (
            <span className="origin-none">לא נבחר (בלי זמני נסיעה)</span>
          )}
          <button type="button" className="origin-btn" onClick={locate} disabled={mode === "locating"}>
            {mode === "locating" ? "מאתר…" : "המיקום שלי"}
          </button>
          <button type="button" className="origin-btn" onClick={() => setMode("address")}>
            כתובת
          </button>
        </>
      )}
      {error && <span className="origin-error">{error}</span>}
    </div>
  );
}

function AddressSearch({ onPick, onCancel }: { onPick: (p: Place) => void; onCancel: () => void }) {
  const id = useId();
  const [q, setQ] = useState("");
  const [places, setPlaces] = useState<Place[]>([]);
  const [state, setState] = useState<"idle" | "searching" | "empty" | "error">("idle");
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    input.current?.focus();
  }, []);

  useEffect(() => {
    const query = q.trim();
    if (query.length < MIN_QUERY) {
      setPlaces([]);
      setState("idle");
      return;
    }
    const ctrl = new AbortController();
    const timer = setTimeout(() => {
      setState("searching");
      api.geocode(query, ctrl.signal).then(
        (r) => {
          setPlaces(r.places);
          setState(r.status === "ok" ? "idle" : r.status === "empty" ? "empty" : "error");
        },
        (err: unknown) => {
          if ((err as Error).name !== "AbortError") setState("error");
        },
      );
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [q]);

  return (
    <div className="address">
      <label htmlFor={id} className="sr-only">
        כתובת או יישוב
      </label>
      <input
        id={id}
        ref={input}
        value={q}
        maxLength={200}
        autoComplete="off"
        placeholder="כתובת או יישוב, למשל: הרצל 10 חיפה"
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") onCancel();
          if (e.key === "Enter") {
            e.preventDefault();
            if (places[0]) onPick(places[0]);
          }
        }}
      />
      <button type="button" className="origin-btn" onClick={onCancel}>
        ביטול
      </button>
      {(places.length > 0 || state !== "idle") && (
        <ul className="suggestions" role="listbox">
          {state === "searching" && <li className="muted">מחפש…</li>}
          {state === "empty" && <li className="muted">לא נמצאה כתובת כזו</li>}
          {state === "error" && <li className="muted">חיפוש הכתובות לא זמין כרגע</li>}
          {state !== "searching" &&
            places.map((p) => (
              <li key={`${p.lat},${p.lon}`}>
                <button type="button" onClick={() => onPick(p)}>
                  {p.label}
                </button>
              </li>
            ))}
        </ul>
      )}
    </div>
  );
}
