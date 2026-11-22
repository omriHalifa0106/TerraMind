import L from "leaflet";
import { useEffect, useState } from "react";
import { GeoJSON, MapContainer, Marker, Polyline, Popup, TileLayer, useMap } from "react-leaflet";
import { api } from "../api/client";
import type { CandidateAssessment, RoutePlan, TrailFeature } from "../api/types";
import type { VetoedTrail } from "../hooks/useChat";
import { hhmm } from "../labels";
import { Clock, Sun } from "./icons";

const CHOSEN_STYLE = { color: "#1E4D3A", weight: 6, opacity: 0.95 };
const VETO_STYLE = { color: "#B4442A", weight: 3, dashArray: "6 7", opacity: 0.85 };

const DRIVE_STYLE = { color: "#2E6F8E", weight: 4, opacity: 0.75, dashArray: "2 8", lineCap: "round" as const };
const originIcon = L.divIcon({ className: "", html: '<div class="origin-pin"></div>', iconSize: [18, 18] });

const stopIcon = (n: number, last: boolean) =>
  L.divIcon({ className: "", html: `<div class="stop-pin${last ? " end" : ""}">${n}</div>`, iconSize: [28, 28] });

/** Loads a trail's GeoJSON from the API (null until loaded or if missing). */
function useTrail(trailId: string | undefined): TrailFeature | null {
  const [feature, setFeature] = useState<TrailFeature | null>(null);
  useEffect(() => {
    let alive = true;
    setFeature(null);
    if (trailId) void api.trail(trailId).then((f) => alive && setFeature(f));
    return () => {
      alive = false;
    };
  }, [trailId]);
  return feature;
}

/** Zooms the map to the chosen trail and its stops whenever the plan changes. */
function FitToPlan({ plan, feature }: { plan: RoutePlan | null; feature: TrailFeature | null }) {
  const map = useMap();
  useEffect(() => {
    if (!plan) return;
    const bounds = L.latLngBounds(plan.stops.map((s) => s.location));
    if (feature) bounds.extend(L.geoJSON(feature).getBounds());
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [48, 48] });
  }, [map, plan, feature]);
  return null;
}

function VetoedLayer({ trail }: { trail: VetoedTrail }) {
  const feature = useTrail(trail.trailId);
  if (!feature) return null;
  return (
    <GeoJSON data={feature} style={VETO_STYLE}>
      <Popup>
        <b>{feature.properties.name_he || feature.properties.name}</b>
        {trail.reasons.map((r) => (
          <div key={r}>{r}</div>
        ))}
      </Popup>
    </GeoJSON>
  );
}

interface Props {
  plan: RoutePlan | null;
  vetoed: VetoedTrail[];
  chosen: CandidateAssessment | null; // agent 3's verdict on the planned trail (weather card)
}

export function TrailMap({ plan, vetoed, chosen }: Props) {
  const feature = useTrail(plan?.trail_id);
  const w = chosen?.weather;
  const hasWeather = w && (w.status === "ok" || w.status === "cached");
  return (
    <section className="map-wrap" aria-label="מפה">
      <MapContainer center={[33.0, 35.55]} zoom={10} className="map">
        <TileLayer
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          attribution="&copy; OpenStreetMap contributors"
          maxZoom={18}
        />
        {vetoed.map((v) => (
          <VetoedLayer key={v.trailId} trail={v} />
        ))}
        {plan && plan.drive_route.length > 1 && <Polyline positions={plan.drive_route} pathOptions={DRIVE_STYLE} />}
        {plan?.origin && (
          <Marker position={plan.origin} icon={originIcon}>
            <Popup>
              <b>{plan.origin_label ?? "נקודת היציאה"}</b>
              {plan.depart_at && <div>יציאה ב-{hhmm(plan.depart_at)}</div>}
            </Popup>
          </Marker>
        )}
        {/* key forces a fresh layer when the trail changes (GeoJSON data is not reactive) */}
        {feature && <GeoJSON key={feature.properties.trail_id} data={feature} style={CHOSEN_STYLE} />}
        {plan?.stops.map((s, i) => (
          <Marker
            key={`${plan.plan_id}-${i}`}
            position={s.location}
            icon={stopIcon(i + 1, i === plan.stops.length - 1)}
          >
            <Popup>
              <b>{s.name}</b>
              <div>{s.arrive_at?.slice(0, 5)}</div>
              {s.note && <div>{s.note}</div>}
            </Popup>
          </Marker>
        ))}
        <FitToPlan plan={plan} feature={feature} />
      </MapContainer>

      <div className="map-overlay">
        {plan && hasWeather && (
          <div className="map-card">
            <Sun className="icon sun" />
            <div>
              <strong>
                {w.temp_max_c ?? "?"}° · גשם {w.precip_prob_max_pct ?? "?"}%
              </strong>
              <span>{w.sunset ? `שקיעה ${hhmm(w.sunset)}` : "תחזית ליום הטיול"}</span>
            </div>
          </div>
        )}
        {plan && (
          <div className="map-card">
            <Clock className="icon" />
            <div>
              <strong>
                {hhmm(plan.start_time)}–{hhmm(plan.end_time)}
              </strong>
              <span>{plan.stops.length} תחנות</span>
            </div>
          </div>
        )}
        <div className="map-card legend">
          <span>
            <i className="sw chosen" />
            מסלול נבחר
          </span>
          <span>
            <i className="sw vetoed" />
            נפסל בבדיקת בטיחות
          </span>
          {plan && plan.drive_route.length > 1 && (
            <span>
              <i className="sw drive" />
              נסיעה
            </span>
          )}
        </div>
      </div>
    </section>
  );
}
